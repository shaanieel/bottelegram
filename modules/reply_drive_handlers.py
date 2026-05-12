"""Reply-link Telegram Index -> Google Drive handlers.

Fitur utama:
- Reply pesan berisi banyak link Telegram Index, lalu ketik:
      /m
  Bot akan menampilkan tombol "Upload ke Google Drive".
- Support link:
      https://zindex.../zEu6c/5126/view
      https://zindex.../zEu6c/5126/Nama.File.mkv
- Link /view akan di-resolve dulu menjadi direct download URL dari tombol
  "DOWNLOAD NOW" di halaman tersebut.
- Setelah tombol Drive diklik, bot enqueue JobType.MIRROR_GDRIVE.
  Worker lama kamu tetap dipakai, jadi cookies/login TGIndex tetap support.

File ini sengaja tidak mengubah telegram_handlers.py.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .logger import get_logger
from .queue_manager import JobType
from .storage_manager import human_bytes
from .tgindex_downloader import TGIndexClient, TGIndexCredentials
from .validators import safe_filename

log = get_logger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def install_reply_drive_handlers(bot_app) -> None:
    """Install reply /m handler dan callback Drive.

    group=-2 supaya handler ini jalan sebelum /m bawaan.
    Kalau /m bukan reply ke pesan berisi URL, handler ini return biasa dan /m
    bawaan tetap berjalan.
    """
    if getattr(bot_app, "_reply_drive_handlers_installed", False):
        return

    bot_app._reply_drive_handlers_installed = True
    bot_app._reply_drive_pending = {}

    app = bot_app.application
    app.add_handler(CommandHandler("m", _cmd_reply_m(bot_app)), group=-2)
    app.add_handler(
        CallbackQueryHandler(_cb_reply_drive(bot_app), pattern=r"^replydrive:"),
        group=-2,
    )

    log.info("Reply Drive handlers installed")


def _is_admin(bot_app, update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in bot_app.cfg.secrets.admin_telegram_ids)


def _cmd_reply_m(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Kalau bukan admin, biarkan handler bawaan admin_only yang jawab.
        if not _is_admin(bot_app, update):
            return

        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        if msg is None or chat is None or user is None:
            return

        # Fitur ini khusus: reply pesan berisi banyak link lalu ketik /m.
        # Kalau /m ada argumen URL biasa, biarkan /m bawaan yang handle.
        raw_text = msg.text or ""
        parts = raw_text.split(maxsplit=1)
        has_args = len(parts) > 1 and bool(parts[1].strip())
        if has_args:
            return

        replied = msg.reply_to_message
        if replied is None:
            return

        source_text = replied.text or replied.caption or ""
        urls = _extract_urls(source_text)
        if not urls:
            return

        pending_id = f"{user.id:x}{len(bot_app._reply_drive_pending):x}"[-10:]
        bot_app._reply_drive_pending[pending_id] = {
            "chat_id": chat.id,
            "user_id": user.id,
            "requested_by": user.username or user.full_name,
            "urls": urls,
        }

        preview = "\n".join(f"• <code>{html.escape(u)}</code>" for u in urls[:10])
        if len(urls) > 10:
            preview += f"\n… dan {len(urls) - 10} link lainnya."

        text = (
            "<b>Link terdeteksi dari pesan reply</b>\n\n"
            f"Jumlah link: <b>{len(urls)}</b>\n"
            f"{preview}\n\n"
            "Pilih target:"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "☁️ Upload ke Google Drive",
                        callback_data=f"replydrive:gdrive:{pending_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬇️ Download saja",
                        callback_data=f"replydrive:download:{pending_id}",
                    ),
                    InlineKeyboardButton(
                        "✕ Batal",
                        callback_data=f"replydrive:cancel:{pending_id}",
                    ),
                ],
            ]
        )

        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        # Stop agar /m bawaan tidak ikut membalas format error.
        raise ApplicationHandlerStop

    return handler


def _cb_reply_drive(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return

        if not _is_admin(bot_app, update):
            await query.answer("Akses ditolak.", show_alert=True)
            return

        try:
            _, action, pending_id = query.data.split(":", 2)
        except ValueError:
            await query.answer("Callback tidak dikenali.", show_alert=True)
            return

        pending = bot_app._reply_drive_pending.pop(pending_id, None)
        if pending is None:
            await query.answer("Pilihan kadaluarsa. Reply link lalu ketik /m lagi.", show_alert=True)
            return

        if action == "cancel":
            await query.answer("Dibatalkan.")
            try:
                await query.edit_message_text("Dibatalkan.")
            except Exception:
                pass
            return

        urls = pending["urls"]
        chat_id = pending["chat_id"]
        user_id = pending["user_id"]
        requested_by = pending["requested_by"]

        if action == "gdrive":
            if not bot_app.gdrive_upload.is_configured():
                await query.answer("Google Drive upload belum dikonfigurasi.", show_alert=True)
                return
            job_type = JobType.MIRROR_GDRIVE
            target_name = "Google Drive"
        elif action == "download":
            job_type = JobType.DOWNLOAD_ONLY
            target_name = "Download saja"
        else:
            await query.answer("Aksi tidak dikenal.", show_alert=True)
            return

        await query.answer(f"Memproses {len(urls)} link…")

        sent_msg = query.message
        if sent_msg:
            try:
                await query.edit_message_text(
                    f"⏳ Resolve link Telegram Index…\nTarget: {html.escape(target_name)}\nJumlah: {len(urls)}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        client = _get_tgindex_client(bot_app)

        resolved = []
        failed = []

        for idx, url in enumerate(urls, start=1):
            try:
                item = await _resolve_tgindex_url(client, url)
                resolved.append(item)
            except Exception as exc:
                log.exception("Gagal resolve TGIndex url %s", url)
                failed.append((url, str(exc)))

            if sent_msg and idx % 3 == 0:
                try:
                    await bot_app.application.bot.edit_message_text(
                        chat_id=sent_msg.chat_id,
                        message_id=sent_msg.message_id,
                        text=(
                            f"⏳ Resolve link Telegram Index…\n"
                            f"Target: {html.escape(target_name)}\n"
                            f"Progress: {idx}/{len(urls)}\n"
                            f"Berhasil: {len(resolved)} | Gagal: {len(failed)}"
                        ),
                    )
                except Exception:
                    pass

        job_ids = []
        for item in resolved:
            job = await bot_app.queue.enqueue(
                job_type=job_type,
                title=item["filename"],
                source_url=item["download_url"],
                chat_id=chat_id,
                user_id=user_id,
                requested_by=requested_by,
            )
            job_ids.append(job.job_id)

        lines = [
            f"<b>{len(job_ids)} job ditambahkan</b>",
            f"Target: {html.escape(target_name)}",
        ]

        if resolved:
            lines.append("")
            lines.append("<b>File:</b>")
            for item in resolved[:10]:
                size = f" — {html.escape(item['size_text'])}" if item.get("size_text") else ""
                lines.append(f"• {html.escape(item['filename'][:70])}{size}")
            if len(resolved) > 10:
                lines.append(f"… dan {len(resolved) - 10} file lainnya.")

        if failed:
            lines.append("")
            lines.append(f"<b>Gagal resolve:</b> {len(failed)} link")
            for url, err in failed[:3]:
                lines.append(f"• <code>{html.escape(url[:80])}</code>")
                lines.append(f"  <i>{html.escape(err[:120])}</i>")

        if job_ids:
            lines.append("")
            lines.append(f"Job pertama: <code>{html.escape(job_ids[0])}</code>")
            lines.append("Lihat live: /queue_live")

        if sent_msg:
            try:
                await bot_app.application.bot.edit_message_text(
                    chat_id=sent_msg.chat_id,
                    message_id=sent_msg.message_id,
                    text="\n".join(lines),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        raise ApplicationHandlerStop

    return handler


def _extract_urls(text: str) -> list[str]:
    out = []
    seen = set()

    for m in URL_RE.finditer(text or ""):
        url = m.group(0).strip().rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        out.append(url)

    return out


def _get_tgindex_client(bot_app) -> TGIndexClient:
    cli = getattr(bot_app, "_reply_drive_tgindex_client", None)
    if cli is not None:
        return cli

    creds = None
    secrets = bot_app.cfg.secrets
    if secrets.tgindex_username and secrets.tgindex_password:
        creds = TGIndexCredentials(
            username=secrets.tgindex_username,
            password=secrets.tgindex_password,
        )

    cli = TGIndexClient(
        credentials=creds,
        user_agent=bot_app.cfg.download.user_agent,
    )
    bot_app._reply_drive_tgindex_client = cli
    return cli


async def _resolve_tgindex_url(client: TGIndexClient, url: str) -> dict:
    """Return {filename, download_url, size_text}.

    Kalau URL sudah direct file, tidak perlu fetch halaman.
    Kalau URL /view, fetch halaman dan cari tombol DOWNLOAD NOW.
    """
    parsed = urlparse(url)
    path = unquote(parsed.path or "")
    name = Path(path).name

    if name and name.lower() != "view" and "." in name:
        filename = safe_filename(name) or name
        return {
            "filename": filename,
            "download_url": url,
            "size_text": "",
        }

    html_body = await client._get_text(url)  # pakai cookie login TGIndex
    soup = BeautifulSoup(html_body, "html.parser")

    filename = _extract_filename_from_view(soup, url)
    size_text = _extract_size_from_view(soup)
    download_url = _extract_download_url_from_view(soup, html_body, url, filename)

    if not download_url:
        raise RuntimeError("Tidak menemukan link DOWNLOAD NOW di halaman /view")

    if not filename:
        filename = safe_filename(Path(unquote(urlparse(download_url).path)).name) or "download.mkv"

    return {
        "filename": filename,
        "download_url": download_url,
        "size_text": size_text,
    }


def _extract_filename_from_view(soup: BeautifulSoup, url: str) -> str:
    selectors = [
        "h1",
        "h2",
        ".filename",
        ".file-name",
        ".title",
    ]

    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if "." in text and len(text) > 3:
            return safe_filename(text) or text

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "." in title:
        return safe_filename(title) or title

    return ""


def _extract_size_from_view(soup: BeautifulSoup) -> str:
    body_text = " ".join(soup.get_text(" ", strip=True).split())
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GiB|MiB|KiB|GB|MB|KB)", body_text, re.I)
    return m.group(0) if m else ""


def _extract_download_url_from_view(
    soup: BeautifulSoup,
    html_body: str,
    base_url: str,
    filename: str,
) -> str:
    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc

    candidates = []

    for a in soup.select("a[href]"):
        href = str(a.get("href") or "").strip()
        text = a.get_text(" ", strip=True).lower()
        if not href:
            continue

        full = urljoin(base_url, href)
        parsed = urlparse(full)
        path = unquote(parsed.path or "")

        if path.rstrip("/").endswith("/view"):
            continue

        base_name = Path(path).name
        looks_file = "." in base_name and len(base_name) > 3
        same_host = parsed.netloc == base_netloc

        score = 0
        if "download" in text:
            score += 5
        if "download" in href.lower():
            score += 2
        if looks_file:
            score += 5
        if same_host:
            score += 1
        if filename and safe_filename(base_name) == safe_filename(filename):
            score += 5

        if score >= 5:
            candidates.append((score, full))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Fallback regex: cari path /channel/id/filename.ext di HTML.
    # Contoh: /zEu6c/5126/Scissors.2026....mkv
    m = re.search(
        r"""["']([^"']+/[A-Za-z0-9_-]+/\d+/[^"']+\.(?:mkv|mp4|avi|mov|webm|m4v))["']""",
        html_body,
        re.I,
    )
    if m:
        return urljoin(base_url, m.group(1))

    return ""
