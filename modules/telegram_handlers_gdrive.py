"""Handler tambahan untuk fitur mirror Telegram Index → Google Drive.

Modul ini di-wire ke ``BotApp`` lewat :func:`register_gdrive_handlers` dan
:func:`run_mirror_gdrive`. Tidak perlu ada manual paste ke ``BotApp``.

Aliran:

1. ``register_gdrive_handlers(app, bot_app)`` dipanggil dari
   ``BotApp._register_handlers``. Fungsi ini mendaftarkan ``/gdrive_list`` dan
   ``/mirror_gdrive`` sebagai command bot, di mana setiap handler memanggil
   ``cmd_gdrive_list`` / ``cmd_mirror_gdrive`` di sini dengan instance
   ``BotApp`` sebagai argumen pertama.
2. ``cmd_mirror_gdrive`` scrape Telegram Index, filter berdasarkan keyword,
   lalu enqueue satu job ``JobType.MIRROR_GDRIVE`` per file. Job source_url
   adalah URL download direct dari index, jadi worker download() biasa di
   ``BotApp._run_job`` bisa menarik filenya seperti file URL biasa.
3. Setelah download selesai, ``_run_job`` mendelegasikan ke
   :func:`run_mirror_gdrive` untuk upload file lokal ke folder Google Drive.
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

from .gdrive_uploader import GDriveUploadError
from .logger import get_logger
from .queue_manager import Job, JobStatus, JobType
from .storage_manager import human_bytes
from .tgindex_downloader import TGIndexError, scrape_index
from .validators import is_url, safe_filename

log = get_logger(__name__)


# ----- Registrasi handler --------------------------------------------------- #


def register_gdrive_handlers(app: Application, bot_app) -> None:
    """Daftarkan ``/gdrive_list`` dan ``/mirror_gdrive`` ke application.

    Setiap command memerlukan akses ke ``BotApp`` (untuk queue, config, dan
    uploader). Karena ``CommandHandler`` hanya mau menerima callable dengan
    signature ``(update, context)``, kita bungkus pemanggilan ke fungsi
    modul-level dengan ``bot_app`` di-bind sebagai closure.
    """

    async def _gdrive_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await cmd_gdrive_list(bot_app, update, context)

    async def _mirror_gdrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await cmd_mirror_gdrive(bot_app, update, context)

    app.add_handler(CommandHandler("gdrive_list", _gdrive_list))
    app.add_handler(CommandHandler("mirror_gdrive", _mirror_gdrive))


# ----- Auth helper ---------------------------------------------------------- #


def _is_admin(bot_app, update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in bot_app.cfg.secrets.admin_telegram_ids


# ----- Command: /gdrive_list ------------------------------------------------ #
# Format: /gdrive_list <index_url> [| keyword]
# Contoh: /gdrive_list https://zindex.aioarea.us.kg/zEu6c | vmx


async def cmd_gdrive_list(
    bot_app, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(bot_app, update):
        await msg.reply_text("Akses ditolak. Bot ini hanya untuk admin.")
        return

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
        f"Scraping index{hint}\u2026", parse_mode=ParseMode.HTML
    )
    try:
        files = await scrape_index(
            index_url,
            keyword_filter=keyword or None,
            user_agent=bot_app.cfg.download.user_agent,
        )
    except TGIndexError as exc:
        await sent.edit_text(f"Gagal scrape index: {exc}")
        return

    if not files:
        text = "Tidak ada file ditemukan"
        if keyword:
            text += f" dengan keyword <b>{html.escape(keyword)}</b>"
        await sent.edit_text(text + ".", parse_mode=ParseMode.HTML)
        return

    lines = [
        f"<b>Ditemukan {len(files)} file</b>"
        + (f" \u2014 filter: <code>{html.escape(keyword)}</code>" if keyword else "")
    ]
    for f in files[:20]:
        lines.append(
            f"{f.position + 1}. <code>{html.escape(f.filename[:60])}</code>"
            f" \u2014 {html.escape(f.size_text)}"
        )
    if len(files) > 20:
        lines.append(f"\u2026 dan {len(files) - 20} file lainnya.")
    lines.append(
        "\n<i>Gunakan /mirror_gdrive untuk download semua ke Drive.</i>"
    )

    await sent.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ----- Command: /mirror_gdrive ---------------------------------------------- #
# Format: /mirror_gdrive <index_url> [| keyword] [| drive_folder_id]
#
# Contoh:
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx
#   /mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx | 1BxFolderIdDrive


async def cmd_mirror_gdrive(
    bot_app, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(bot_app, update):
        await msg.reply_text("Akses ditolak. Bot ini hanya untuk admin.")
        return

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
            "<code>/mirror_gdrive https://zindex.aioarea.us.kg/zEu6c | vmx | "
            "1BxFolderIdDrive</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    index_url, keyword, drive_folder_id = _parse_pipe_args(args, n=3)

    # Kalau folder_id tidak diisi dari command, ambil dari config
    if not drive_folder_id:
        drive_folder_id = bot_app.cfg.gdrive_upload.default_folder_id or ""

    if not is_url(index_url):
        await msg.reply_text(
            "URL index tidak valid. Harus diawali http:// atau https://"
        )
        return

    if not bot_app.gdrive_upload.is_configured():
        await msg.reply_text(
            "Google Drive upload belum dikonfigurasi.\n"
            "Set <code>GOOGLE_DRIVE_OAUTH_TOKEN_PATH</code> di .env "
            "(atau <code>GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    hint = f" (filter: {keyword})" if keyword else ""
    sent = await msg.reply_text(f"Scraping index{hint}\u2026")

    try:
        files = await scrape_index(
            index_url,
            keyword_filter=keyword or None,
            user_agent=bot_app.cfg.download.user_agent,
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

    # Enqueue satu job per file. Worker di BotApp._run_job akan handle
    # download (lewat downloader.direct) dan dispatch ke run_mirror_gdrive.
    job_ids: list[str] = []
    for tg_file in files:
        job = await bot_app.queue.enqueue(
            job_type=JobType.MIRROR_GDRIVE,
            title=safe_filename(tg_file.filename) or tg_file.filename,
            source_url=tg_file.download_url,
            chat_id=chat_id,
            user_id=user.id if user else None,
            requested_by=(user.username or user.full_name) if user else None,
        )
        # Simpan drive_folder_id + keyword di job supaya worker bisa baca.
        # ``progress_text`` di-encode sebagai "GDRIVE_FOLDER:<id>" dan akan
        # ditimpa oleh status update berikutnya — kita tidak menyimpan ID
        # di sana untuk jangka panjang.
        await bot_app.queue._update(  # type: ignore[attr-defined]
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
        lines.append(
            f"\u2022 {html.escape(f.filename[:55])} ({f.size_text})"
        )
    if len(files) > 10:
        lines.append(f"\u2026 dan {len(files) - 10} file lagi")
    lines.append(f"\nJob pertama: <code>{job_ids[0]}</code>")

    await sent.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # Pasang progress message untuk job pertama supaya user lihat live update.
    if job_ids and chat_id:
        bot_app._progress_msg[job_ids[0]] = (chat_id, sent.message_id)


# ----- Worker: run_mirror_gdrive -------------------------------------------- #


async def run_mirror_gdrive(
    bot_app,
    job: Job,
    file_path: Path,
    progress: Callable[[str, Optional[float], str], Awaitable[None]],
    cancel_event: asyncio.Event,
) -> None:
    """Worker untuk JobType.MIRROR_GDRIVE — upload file lokal ke Google Drive.

    Folder tujuan diambil dari ``job.progress_text`` (encoded sebagai
    ``"GDRIVE_FOLDER:<id>"`` saat enqueue), atau dari config default kalau
    tidak ada.
    """

    drive_folder_id: Optional[str] = None
    pt = job.progress_text or ""
    if pt.startswith("GDRIVE_FOLDER:"):
        raw_folder = pt.replace("GDRIVE_FOLDER:", "").strip()
        drive_folder_id = raw_folder if raw_folder else None

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

    await bot_app.queue._update(  # type: ignore[attr-defined]
        job,
        gdrive_file_id=file_id,
        gdrive_web_link=web_link,
        status=JobStatus.COMPLETED,
        finished_at=time.time(),
        progress=100.0,
        progress_text=f"Upload Drive selesai. File ID: {file_id}",
    )

    # File lokal akan dihapus oleh ``_run_job`` (auto_delete_after_upload),
    # jadi tidak perlu safe_unlink di sini supaya tidak double-delete.
    # Notifikasi completion juga dikirim oleh ``_send_completion`` di BotApp.


# ----- Helpers -------------------------------------------------------------- #


def _parse_pipe_args(text: str, n: int = 3) -> tuple:
    """Split 'a | b | c' jadi tuple panjang n; sisa diisi string kosong."""
    parts = [p.strip() for p in text.split("|", maxsplit=n - 1)]
    while len(parts) < n:
        parts.append("")
    return tuple(parts[:n])


def _parse_size_text(size_text: str) -> int:
    """Parse '1.05 GiB' atau '500 MiB' → bytes (best-effort)."""
    if not size_text:
        return 0
    m = re.match(
        r"([\d.]+)\s*(GiB|MiB|KiB|GB|MB|KB|B)", size_text.strip(), re.I
    )
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000 ** 2,
        "MIB": 1024 ** 2,
        "GB": 1000 ** 3,
        "GIB": 1024 ** 3,
    }
    return int(val * mult.get(unit, 1))
