"""Advanced /m panel for Telegram Index and Google Drive mirroring.

This module adds a richer Mirror flow without rewriting the old BotApp class:

- Reply a message containing many Telegram Index /view links, then send /m.
- Or send /m URL.
- Pick destination folder preset: ZaeinBot or VMX Zaein.
- Pick upload target: Google Drive, Player4Me, or download only.
- Enqueue Google Drive mirror jobs with the selected folder_id persisted in Job.

The actual downloader/uploader pipeline remains the existing queue worker.
"""

from __future__ import annotations

import html
import re
import time
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
from .tgindex_downloader import TGIndexClient, TGIndexCredentials
from .validators import parse_command_args, safe_filename

log = get_logger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\"']+")

FOLDER_PRESETS = {
    "zaeinbot": {
        "label": "📁 ZaeinBot",
        "folder_name": "ZaeinBot",
        "subfolder_name": "",
    },
    "vmx": {
        "label": "🎬 VMX Zaein",
        "folder_name": "ZaeinBot",
        "subfolder_name": "VMX Zaein",
    },
}


def install_advanced_mirror_handlers(bot_app) -> None:
    """Install advanced /m handlers.

    group=-3 so this intercepts /m before reply_drive_handlers and legacy /m.
    It only stops the handler chain when it actually handled the message.
    """
    if getattr(bot_app, "_advanced_mirror_handlers_installed", False):
        return

    bot_app._advanced_mirror_handlers_installed = True
    bot_app._advanced_mirror_pending = {}
    bot_app._advanced_mirror_tgindex_client = None

    app = bot_app.application
    app.add_handler(CommandHandler("m", _cmd_mirror_panel(bot_app)), group=-3)
    app.add_handler(
        CallbackQueryHandler(_cb_mirror_panel(bot_app), pattern=r"^advmm:"),
        group=-3,
    )

    log.info("Advanced mirror handlers installed")


def _is_admin(bot_app, update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in bot_app.cfg.secrets.admin_telegram_ids)


def _cmd_mirror_panel(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(bot_app, update):
            return

        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None or user is None:
            return

        raw = msg.text or ""
        parts = raw.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""

        urls: list[str] = []
        title_hint = ""

        if args:
            maybe_title, maybe_url = parse_command_args(args)
            if maybe_url:
                title_hint = maybe_title or ""
                urls = _extract_urls(maybe_url)
            else:
                urls = _extract_urls(args)
        elif msg.reply_to_message:
            source_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
            urls = _extract_urls(source_text)

        # Only handle when user gave/replied URLs. Otherwise let legacy /m show help.
        if not urls:
            return

        pending_id = f"{user.id:x}{int(time.time() * 1000):x}"[-12:]
        bot_app._advanced_mirror_pending[pending_id] = {
            "chat_id": chat.id,
            "user_id": user.id,
            "requested_by": user.username or user.full_name,
            "urls": urls,
            "title_hint": title_hint,
            "folder_key": "zaeinbot",
            "target": "gdrive",
            "created_at": time.time(),
        }

        await msg.reply_text(
            _render_panel(bot_app, pending_id),
            parse_mode=ParseMode.HTML,
            reply_markup=_panel_keyboard(pending_id, folder_key="zaeinbot", target="gdrive"),
            disable_web_page_preview=True,
        )
        raise ApplicationHandlerStop

    return handler


def _cb_mirror_panel(bot_app):
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
            await query.answer("Callback tidak dikenal.", show_alert=True)
            return

        pending = bot_app._advanced_mirror_pending.get(pending_id)
        if not pending:
            await query.answer("Panel kadaluarsa. Kirim /m lagi.", show_alert=True)
            return

        if action.startswith("folder_"):
            key = action.replace("folder_", "", 1)
            if key in FOLDER_PRESETS:
                pending["folder_key"] = key
                await query.answer(f"Folder: {FOLDER_PRESETS[key]['label']}")
                await _edit_panel(bot_app, query, pending_id)
            return

        if action.startswith("target_"):
            target = action.replace("target_", "", 1)
            if target in {"gdrive", "player4me", "download"}:
                pending["target"] = target
                await query.answer(f"Target: {_target_label(target)}")
                await _edit_panel(bot_app, query, pending_id)
            return

        if action == "cancel":
            bot_app._advanced_mirror_pending.pop(pending_id, None)
            await query.answer("Dibatalkan.")
            try:
                await query.edit_message_text("❌ Mirror dibatalkan.")
            except Exception:
                pass
            return

        if action == "start":
            await query.answer("Mulai mirror…")
            await _start_pending_jobs(bot_app, query, pending_id, pending)
            return

        await query.answer("Aksi tidak dikenal.", show_alert=True)

    return handler


async def _edit_panel(bot_app, query, pending_id: str) -> None:
    pending = bot_app._advanced_mirror_pending[pending_id]
    try:
        await query.edit_message_text(
            _render_panel(bot_app, pending_id),
            parse_mode=ParseMode.HTML,
            reply_markup=_panel_keyboard(
                pending_id,
                folder_key=pending.get("folder_key", "zaeinbot"),
                target=pending.get("target", "gdrive"),
            ),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.debug("edit advanced panel failed: %s", exc)


def _render_panel(bot_app, pending_id: str) -> str:
    pending = bot_app._advanced_mirror_pending[pending_id]
    urls = pending["urls"]
    folder_key = pending.get("folder_key", "zaeinbot")
    target = pending.get("target", "gdrive")
    preset = FOLDER_PRESETS.get(folder_key, FOLDER_PRESETS["zaeinbot"])

    preview = "\n".join(f"• <code>{html.escape(u[:82])}</code>" for u in urls[:5])
    if len(urls) > 5:
        preview += f"\n… dan {len(urls) - 5} link lainnya."

    return (
        "<b>📌 Pilih option untuk tugas mirror</b>\n\n"
        f"<b>Jumlah link:</b> {len(urls)}\n"
        f"<b>Folder Drive:</b> {html.escape(preset['label'])}\n"
        f"<b>Upload:</b> {_target_label(target)}\n"
        "<b>Mode:</b> #Mirror #TelegramIndex\n\n"
        f"{preview}\n\n"
        "Klik <b>START MIRROR</b> kalau pilihan sudah benar."
    )


def _panel_keyboard(pending_id: str, *, folder_key: str, target: str) -> InlineKeyboardMarkup:
    def mark(value: str, selected: str, label: str) -> str:
        return ("✅ " if value == selected else "") + label

    rows = [
        [
            InlineKeyboardButton("▶️ START MIRROR", callback_data=f"advmm:start:{pending_id}"),
        ],
        [
            InlineKeyboardButton(
                mark("zaeinbot", folder_key, "📁 ZaeinBot"),
                callback_data=f"advmm:folder_zaeinbot:{pending_id}",
            ),
            InlineKeyboardButton(
                mark("vmx", folder_key, "🎬 VMX Zaein"),
                callback_data=f"advmm:folder_vmx:{pending_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                mark("gdrive", target, "☁️ Drive"),
                callback_data=f"advmm:target_gdrive:{pending_id}",
            ),
            InlineKeyboardButton(
                mark("player4me", target, "▶️ Player4Me"),
                callback_data=f"advmm:target_player4me:{pending_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                mark("download", target, "⬇️ Download saja"),
                callback_data=f"advmm:target_download:{pending_id}",
            ),
            InlineKeyboardButton("🚫 Batal", callback_data=f"advmm:cancel:{pending_id}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _start_pending_jobs(bot_app, query, pending_id: str, pending: dict) -> None:
    urls: list[str] = pending["urls"]
    target = pending.get("target", "gdrive")
    folder_key = pending.get("folder_key", "zaeinbot")

    if target == "gdrive" and not bot_app.gdrive_upload.is_configured():
        await query.answer("Google Drive upload belum dikonfigurasi.", show_alert=True)
        return
    if target == "player4me" and not bot_app.player4me.is_configured():
        await query.answer("Player4Me belum dikonfigurasi.", show_alert=True)
        return

    sent_msg = query.message
    if sent_msg:
        await query.edit_message_text(
            f"⏳ Resolve link…\nTarget: {_target_label(target)}\nJumlah: {len(urls)}",
            parse_mode=ParseMode.HTML,
        )

    client = _get_tgindex_client(bot_app)
    folder_id: Optional[str] = None
    if target == "gdrive":
        folder_id = await _ensure_drive_folder(bot_app, folder_key)

    resolved = []
    failed = []
    for idx, url in enumerate(urls, start=1):
        try:
            resolved.append(await _resolve_source_url(client, url))
        except Exception as exc:
            log.exception("resolve source failed: %s", url)
            failed.append((url, str(exc)))

        if sent_msg and idx % 3 == 0:
            try:
                await bot_app.application.bot.edit_message_text(
                    chat_id=sent_msg.chat_id,
                    message_id=sent_msg.message_id,
                    text=(
                        f"⏳ Resolve link… {idx}/{len(urls)}\n"
                        f"Berhasil: {len(resolved)} | Gagal: {len(failed)}"
                    ),
                )
            except Exception:
                pass

    job_type = {
        "gdrive": JobType.MIRROR_GDRIVE,
        "player4me": JobType.UPLOAD_PLAYER4ME,
        "download": JobType.DOWNLOAD_ONLY,
    }[target]

    job_ids = []
    for item in resolved:
        job = await bot_app.queue.enqueue(
            job_type=job_type,
            title=item["filename"],
            source_url=item["download_url"],
            chat_id=pending["chat_id"],
            user_id=pending["user_id"],
            requested_by=pending["requested_by"],
        )
        if target == "gdrive" and folder_id:
            await bot_app.queue._update(job, gdrive_target_folder_id=folder_id)
        job_ids.append(job.job_id)

    bot_app._advanced_mirror_pending.pop(pending_id, None)

    lines = [
        f"<b>✅ {len(job_ids)} tugas masuk antrean</b>",
        f"Target: {_target_label(target)}",
    ]
    if folder_id:
        lines.append(f"Folder ID: <code>{html.escape(folder_id)}</code>")
    if resolved:
        lines.append("\n<b>File:</b>")
        for item in resolved[:8]:
            lines.append(f"• {html.escape(item['filename'][:75])}")
        if len(resolved) > 8:
            lines.append(f"… dan {len(resolved) - 8} file lainnya.")
    if failed:
        lines.append(f"\n<b>Gagal resolve:</b> {len(failed)} link")
    if job_ids:
        lines.append(f"\nJob pertama: <code>{html.escape(job_ids[0])}</code>")
        lines.append("Live monitor: /queue_live")

    if sent_msg:
        await bot_app.application.bot.edit_message_text(
            chat_id=sent_msg.chat_id,
            message_id=sent_msg.message_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


def _target_label(target: str) -> str:
    return {
        "gdrive": "☁️ Google Drive",
        "player4me": "▶️ Player4Me",
        "download": "⬇️ Download saja",
    }.get(target, target)


async def _ensure_drive_folder(bot_app, folder_key: str) -> Optional[str]:
    preset = FOLDER_PRESETS.get(folder_key, FOLDER_PRESETS["zaeinbot"])
    root_name = preset["folder_name"]
    sub_name = preset.get("subfolder_name") or ""

    root_id = await bot_app.gdrive_upload.find_or_create_folder(root_name)
    if sub_name:
        return await bot_app.gdrive_upload.find_or_create_folder(
            sub_name,
            parent_folder_id=root_id,
        )
    return root_id


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
    cli = getattr(bot_app, "_advanced_mirror_tgindex_client", None)
    if cli is not None:
        return cli

    creds = None
    secrets = bot_app.cfg.secrets
    if secrets.tgindex_username and secrets.tgindex_password:
        creds = TGIndexCredentials(secrets.tgindex_username, secrets.tgindex_password)
    cli = TGIndexClient(credentials=creds, user_agent=bot_app.cfg.download.user_agent)
    bot_app._advanced_mirror_tgindex_client = cli
    return cli


async def _resolve_source_url(client: TGIndexClient, url: str) -> dict:
    parsed = urlparse(url)
    path = unquote(parsed.path or "")
    name = Path(path).name

    # Google Drive and normal direct URLs are left untouched.
    if "drive.google.com" in parsed.netloc:
        filename = safe_filename(Path(path).stem or "Google Drive File")
        return {"filename": filename or "Google Drive File", "download_url": url}

    # Telegram Index direct file URL.
    if name and name.lower() != "view" and "." in name:
        filename = safe_filename(name) or name
        return {"filename": filename, "download_url": url}

    # Telegram Index /view page; resolve DOWNLOAD NOW target.
    html_body = await client._get_text(url)
    soup = BeautifulSoup(html_body, "html.parser")
    filename = _extract_filename_from_view(soup)
    download_url = _extract_download_url_from_view(soup, html_body, url, filename)
    if not download_url:
        raise RuntimeError("Tidak menemukan link download di halaman /view")
    if not filename:
        filename = safe_filename(Path(unquote(urlparse(download_url).path)).name) or "download.mkv"
    return {"filename": filename, "download_url": download_url}


def _extract_filename_from_view(soup: BeautifulSoup) -> str:
    for sel in ("h1", "h2", ".filename", ".file-name", ".title"):
        el = soup.select_one(sel)
        if not el:
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if "." in text:
            return safe_filename(text) or text
    return ""


def _extract_download_url_from_view(
    soup: BeautifulSoup,
    html_body: str,
    base_url: str,
    filename: str,
) -> str:
    base_netloc = urlparse(base_url).netloc
    candidates = []
    for a in soup.select("a[href]"):
        href = str(a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        path = unquote(parsed.path or "")
        if path.rstrip("/").endswith("/view"):
            continue
        base_name = Path(path).name
        looks_file = "." in base_name and len(base_name) > 3
        text = a.get_text(" ", strip=True).lower()
        score = 0
        if "download" in text:
            score += 5
        if looks_file:
            score += 5
        if parsed.netloc == base_netloc:
            score += 1
        if filename and safe_filename(base_name) == safe_filename(filename):
            score += 5
        if score >= 5:
            candidates.append((score, full))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    m = re.search(
        r"""[\"']([^\"']+/[A-Za-z0-9_-]+/\d+/[^\"']+\.(?:mkv|mp4|avi|mov|webm|m4v))[\"']""",
        html_body,
        re.I,
    )
    return urljoin(base_url, m.group(1)) if m else ""
