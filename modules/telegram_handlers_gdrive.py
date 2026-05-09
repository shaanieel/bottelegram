"""Tambahan handler untuk fitur mirror TG Index → Google Drive.

Cara integrasi ke telegram_handlers.py yang sudah ada:

1. Tambahkan import di bagian atas telegram_handlers.py:

    from .gdrive_uploader import GDriveUploader, GDriveUploadError
    from .tgindex_downloader import TGIndexFile, TGIndexError, scrape_index
    from .telegram_handlers_gdrive import register_gdrive_handlers, run_mirror_gdrive

2. Di __init__ BotApp, setelah baris `self.player4me = Player4MeUploader(cfg)`:

    self.gdrive_upload = GDriveUploader(cfg)

3. Di _register_handlers, setelah handler terakhir yang sudah ada:

    register_gdrive_handlers(app, self)

4. Di _publish_bot_commands, tambahkan ke list commands:

    BotCommand("gdrive_list", "Preview file di TG Index"),
    BotCommand("mirror_gdrive", "Mirror TG Index → Google Drive"),

5. Di _run_job, setelah blok `elif job.type == JobType.UPLOAD_PLAYER4ME_SUBS.value`:

    elif job.type == JobType.MIRROR_GDRIVE.value:
        await run_mirror_gdrive(self, job, result.path, progress, cancel_event)
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .gdrive_uploader import GDriveUploader, GDriveUploadError
from .logger import get_logger
from .queue_manager import Job, JobStatus, JobType, QueueManager
from .storage_manager import human_bytes, safe_unlink
from .tgindex_downloader import TGIndexError, scrape_index
from .validators import is_url, safe_filename

log = get_logger(__name__)


# ----- Registrasi handler --------------------------------------------------- #

def register_gdrive_handlers(app: Application, bot_app) -> None:
    """Daftarkan command /gdrive_list dan /mirror_gdrive ke application."""
    app.add_handler(CommandHandler("gdrive_list", bot_app.cmd_gdrive_list))
    app.add_handler(CommandHandler("mirror_gdrive", bot_app.cmd_mirror_gdrive))


# ----- Command: /gdrive_list ------------------------------------------------ #
# Format: /gdrive_list <index_url> [| keyword]
# Contoh: /gdrive_list https://zindex.aioarea.us.kg/zEu6c | vmx

async def cmd_gdrive_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    raw = (msg.text or "").split(maxsplit=1)
    args = raw[1].strip() if len(raw) > 1 else ""

    if not args:
        await msg.reply_text(
            "Format: /gdrive_list URL [| keyword]\n\n"
            "Contoh:\n"
            "/gdrive_list https://zindex.aioarea.us.kg/zEu6c\n"
            "/gdrive_list https://zindex.aioarea.us.kg/zEu6c | vmx",
        )
        return

    index_url, keyword = _parse_pipe_args(args, n=2)
    if not is_url(index_url):
        await msg.reply_text("URL index tidak valid.")
        return

    hint = f" (filter: <code>{html.escape(keyword)}</code>)" if keyword else ""
    sent = await msg.reply_text(
        f"Scraping index{hint}…", parse_mode=ParseMode.HTML
    )
    try:
        files = await scrape_index(
            index_url,
            keyword_filter=keyword or None,
            user_agent=self.cfg.download.user_agent,
        )
    except TGIndexError as exc:
        await sent.edit_text(f"Gagal scrape index: {exc}")
        return

    if not files:
        msg_text = "Tidak ada file ditemukan"
        if keyword:
            msg_text += f" dengan keyword <b>{html.escape(keyword)}</b>"
        await sent.edit_text(msg_text + ".", parse_mode=ParseMode.HTML)
        return

    lines = [
        f"<b>Ditemukan {len(files)} file</b>"
        + (f" — filter: <code>{html.escape(keyword)}</code>" if keyword else "")
    ]
    for f in files[:20]:
        lines.append(
            f"{f.position + 1}. <code>{html.escape(f.filename[:60])}</code>"
            f" — {html.escape(f.size_text)}"
        )
    if len(files) > 20:
        lines.append(f"…dan {len(files) - 20} file lainnya.")
    lines.append("\n<i>Gunakan /mirror_gdrive untuk download semua ke Drive.</i>")

    await sent.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ----- Command: /mirror_gdrive ---------------------------------------------- #
# Format: /mirror_gdrive <index_url> [| keyword] [| drive_folder_id]
#
# Contoh:
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx | 1BxFolderIdDrive

async def cmd_mirror_gdrive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    raw = (msg.text or "").split(maxsplit=1)
    args = raw[1].strip() if len(raw) > 1 else ""

    if not args:
        await msg.reply_text(
            "<b>Format:</b>\n"
            "<code>/mirror_gdrive URL [| keyword] [| drive_folder_id]</code>\n\n"
            "<b>Contoh:</b>\n"
            "Download semua:\n"
            "<code>/mirror_gdrive https://zindex.aioarea.us.kg/zEu6c</code>\n\n"
            "Filter keyword 'vmx':\n"
            "<code>/mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx</code>\n\n"
            "Filter + folder Drive tertentu:\n"
            "<code>/mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx | 1BxFolderIdDrive</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    index_url, keyword, drive_folder_id = _parse_pipe_args(args, n=3)

    # Kalau folder_id tidak diisi dari command, ambil dari config
    if not drive_folder_id:
        drive_folder_id = self.cfg.gdrive_upload.default_folder_id or ""

    if not is_url(index_url):
        await msg.reply_text("URL index tidak valid. Harus diawali http:// atau https://")
        return

    if not self.gdrive_upload.is_configured():
        await msg.reply_text(
            "Google Drive upload belum dikonfigurasi.\n"
            "Set <code>GOOGLE_DRIVE_OAUTH_TOKEN_PATH</code> di .env.",
            parse_mode=ParseMode.HTML,
        )
        return

    hint = f" (filter: {keyword})" if keyword else ""
    sent = await msg.reply_text(f"Scraping index{hint}…")

    try:
        files = await scrape_index(
            index_url,
            keyword_filter=keyword or None,
            user_agent=self.cfg.download.user_agent,
        )
    except TGIndexError as exc:
        await sent.edit_text(f"Gagal scrape index: {exc}")
        return

    if not files:
        await sent.edit_text(
            "Tidak ada file yang cocok ditemukan."
            + (f" (keyword: {keyword})" if keyword else "")
        )
        return

    total_size = sum(_parse_size_text(f.size_text) for f in files)
    size_hint = human_bytes(total_size) if total_size else "?"

    chat_id = update.effective_chat.id if update.effective_chat else 0
    user = update.effective_user

    # Enqueue satu job per file — urutan list sudah terbaru dulu (sesuai index)
    job_ids: list[str] = []
    for tg_file in files:
        job = await self.queue.enqueue(
            job_type=JobType.MIRROR_GDRIVE,
            title=safe_filename(tg_file.filename) or tg_file.filename,
            source_url=tg_file.download_url,
            chat_id=chat_id,
            user_id=user.id if user else None,
            requested_by=(user.username or user.full_name) if user else None,
        )
        # Simpan drive_folder_id di progress_text sementara supaya worker bisa baca
        # Format: "GDRIVE_FOLDER:<folder_id>"
        await self.queue._update(
            job,
            progress_text=f"GDRIVE_FOLDER:{drive_folder_id}",
            tgindex_keyword=keyword or None,
        )
        job_ids.append(job.job_id)

    lines = [
        f"<b>{len(job_ids)} job ditambahkan ke antrean</b>",
        f"Estimasi total: {size_hint}",
        f"Folder Drive: <code>{html.escape(drive_folder_id or 'Root Drive')}</code>",
        "",
    ]
    for f in files[:10]:
        lines.append(f"• {html.escape(f.filename[:55])} ({f.size_text})")
    if len(files) > 10:
        lines.append(f"…dan {len(files) - 10} file lagi")
    lines.append(f"\nJob pertama: <code>{job_ids[0]}</code>")

    await sent.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # Pasang progress message untuk job pertama
    if job_ids and chat_id:
        self._progress_msg[job_ids[0]] = (chat_id, sent.message_id)


# ----- Worker: run_mirror_gdrive -------------------------------------------- #

async def run_mirror_gdrive(
    bot_app,
    job: Job,
    file_path: Path,
    progress: Callable[[str, float | None, str], Awaitable[None]],
    cancel_event: asyncio.Event,
) -> None:
    """Worker untuk JobType.MIRROR_GDRIVE — upload file ke Google Drive."""

    # Baca drive_folder_id dari progress_text yang di-encode waktu enqueue
    drive_folder_id: Optional[str] = None
    pt = job.progress_text or ""
    if pt.startswith("GDRIVE_FOLDER:"):
        raw_folder = pt.replace("GDRIVE_FOLDER:", "").strip()
        drive_folder_id = raw_folder if raw_folder else None

    # Fallback ke config
    if not drive_folder_id:
        drive_folder_id = bot_app.cfg.gdrive_upload.default_folder_id or None

    try:
        metadata = await bot_app.gdrive_upload.upload_file(
            file_path,
            folder_id=drive_folder_id,
            filename_override=file_path.name,
            progress_cb=progress,
            cancel_event=cancel_event,
        )
    except GDriveUploadError as exc:
        raise RuntimeError(f"Upload ke Drive gagal: {exc}") from exc

    file_id = metadata.get("id") or ""
    web_link = metadata.get("webViewLink") or ""

    await bot_app.queue._update(
        job,
        gdrive_file_id=file_id,
        gdrive_web_link=web_link,
        status=JobStatus.COMPLETED,
        finished_at=time.time(),
        progress=100.0,
        progress_text=f"Upload Drive selesai. File ID: {file_id}",
    )

    # Auto-delete file lokal setelah upload
    if bot_app.cfg.app.auto_delete_after_upload:
        safe_unlink(file_path)

    # Notifikasi selesai
    if job.chat_id and file_id:
        text = (
            f"<b>Mirror ke Drive Selesai</b>\n\n"
            f"Judul: {html.escape(job.title)}\n"
            f"File ID: <code>{html.escape(file_id)}</code>\n"
        )
        if web_link:
            text += f"Link: {web_link}\n"
        if drive_folder_id:
            text += f"Folder: <code>{html.escape(drive_folder_id)}</code>"
        await bot_app._safe_send(job.chat_id, text)


# ----- Helpers -------------------------------------------------------------- #

def _parse_pipe_args(text: str, n: int = 3) -> tuple:
    """Split 'a | b | c' jadi tuple dengan panjang n, sisa diisi string kosong."""
    parts = [p.strip() for p in text.split("|", maxsplit=n - 1)]
    while len(parts) < n:
        parts.append("")
    return tuple(parts[:n])


def _parse_size_text(size_text: str) -> int:
    """Parse '1.05 GiB' atau '500 MiB' → bytes."""
    if not size_text:
        return 0
    m = re.match(r"([\d.]+)\s*(GiB|MiB|KiB|GB|MB|KB|B)", size_text.strip(), re.I)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {
        "B": 1, "KB": 1000, "KIB": 1024,
        "MB": 1000**2, "MIB": 1024**2,
        "GB": 1000**3, "GIB": 1024**3,
    }
    return int(val * mult.get(unit, 1))
