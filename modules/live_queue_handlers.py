"""Live queue monitor + quick cancel handlers.

Fitur:
- /queue_live
- /cancel_all
- /cancel_<job_id>
- tombol inline Cancel Semua Job
- live queue auto update + animasi loading
- pagination Prev/Next kalau job banyak
- ringkasan system CPU/RAM/disk/network best-effort
"""

from __future__ import annotations

import asyncio
import html
import os
import shutil
import time
from typing import Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .logger import get_logger
from .queue_manager import Job, JobStatus

log = get_logger(__name__)

ACTIVE_STATUSES = {
    JobStatus.DOWNLOADING.value,
    JobStatus.DOWNLOADED.value,
    JobStatus.UPLOADING.value,
    JobStatus.ENCODING.value,
}

CANCELLABLE_STATUSES = {
    JobStatus.QUEUED.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.DOWNLOADED.value,
    JobStatus.UPLOADING.value,
    JobStatus.ENCODING.value,
}

SPINNER_FRAMES = ["⏳", "⌛", "🔄", "⬇️", "📥"]
PAGE_SIZE = 5


def install_live_queue_handlers(bot_app) -> None:
    """Install live queue handlers ke BotApp.

    group=-1 dipakai supaya handler ini diproses sebelum fallback unknown command.
    """
    if getattr(bot_app, "_live_queue_handlers_installed", False):
        return

    bot_app._live_queue_handlers_installed = True
    bot_app._live_queue_messages = {}
    bot_app._live_queue_last_text = {}
    bot_app._live_queue_last_edit = {}
    bot_app._live_queue_spinner_index = 0
    bot_app._live_queue_tasks = {}
    bot_app._live_queue_page = {}
    bot_app._live_queue_net_last = None
    bot_app._live_queue_min_interval = 1.5

    app = bot_app.application

    app.add_handler(CommandHandler("queue_live", _cmd_queue_live(bot_app)), group=-1)
    app.add_handler(CommandHandler("cancel_all", _cmd_cancel_all(bot_app)), group=-1)
    app.add_handler(
        MessageHandler(filters.Regex(r"^/cancel_[A-Za-z0-9]+$"), _cmd_cancel_short(bot_app)),
        group=-1,
    )
    app.add_handler(
        CallbackQueryHandler(_cb_live_queue(bot_app), pattern=r"^livequeue:"),
        group=-1,
    )

    _wrap_queue_status_change(bot_app)
    _wrap_publish_bot_commands(bot_app)

    log.info("Live queue handlers installed")


def _is_admin(bot_app, update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in bot_app.cfg.secrets.admin_telegram_ids)


def _cmd_queue_live(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(bot_app, update):
            if update.effective_message:
                await update.effective_message.reply_text("Akses ditolak. Bot ini hanya untuk admin.")
            return

        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None:
            return

        chat_id = chat.id
        bot_app._live_queue_page[chat_id] = 0
        text = _render_live_queue(bot_app, chat_id=chat_id)

        sent = await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(chat_id, bot_app),
            disable_web_page_preview=True,
        )

        bot_app._live_queue_messages[chat_id] = sent.message_id
        bot_app._live_queue_last_text[chat_id] = text
        bot_app._live_queue_last_edit[chat_id] = time.monotonic()

        _ensure_spinner_task(bot_app, chat_id)

    return handler


def _cmd_cancel_all(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(bot_app, update):
            if update.effective_message:
                await update.effective_message.reply_text("Akses ditolak. Bot ini hanya untuk admin.")
            return

        count = await _cancel_all_jobs(bot_app)

        if update.effective_message:
            await update.effective_message.reply_text(f"✅ Berhasil membatalkan {count} job.")

        await _refresh_live_queue_messages(bot_app, force=True)

    return handler


def _cmd_cancel_short(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(bot_app, update):
            if update.effective_message:
                await update.effective_message.reply_text("Akses ditolak. Bot ini hanya untuk admin.")
            return

        msg = update.effective_message
        if msg is None or not msg.text:
            return

        job_id = msg.text.replace("/cancel_", "", 1).strip()
        ok = await bot_app.queue.cancel(job_id)

        if ok:
            await msg.reply_text(f"✅ Job {job_id} dibatalkan.")
        else:
            await msg.reply_text(f"⚠️ Job {job_id} tidak ditemukan atau sudah selesai/gagal.")

        await _refresh_live_queue_messages(bot_app, force=True)

    return handler


def _cb_live_queue(bot_app):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return

        if not _is_admin(bot_app, update):
            await query.answer("Akses ditolak.", show_alert=True)
            return

        parts = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        chat_id = query.message.chat_id if query.message else None

        if action == "cancel_all":
            count = await _cancel_all_jobs(bot_app)
            await query.answer(f"{count} job dibatalkan.", show_alert=True)
            await _refresh_live_queue_messages(bot_app, force=True)
            return

        if action == "refresh":
            await query.answer("Refresh antrean.")
            await _refresh_live_queue_messages(bot_app, force=True)
            return

        if action in {"next", "prev"} and chat_id is not None:
            all_jobs = _display_jobs(bot_app)
            max_page = max(0, (len(all_jobs) - 1) // PAGE_SIZE)
            cur = int(bot_app._live_queue_page.get(chat_id, 0))
            if action == "next":
                cur = min(max_page, cur + 1)
            else:
                cur = max(0, cur - 1)
            bot_app._live_queue_page[chat_id] = cur
            await query.answer(f"Halaman {cur + 1}/{max_page + 1}")
            await _refresh_live_queue_messages(bot_app, force=True, only_chat_id=chat_id)
            return

        await query.answer("Aksi tidak dikenal.", show_alert=True)

    return handler


async def _cancel_all_jobs(bot_app) -> int:
    count = 0
    for job in bot_app.queue.list_jobs():
        if job.status not in CANCELLABLE_STATUSES:
            continue
        ok = await bot_app.queue.cancel(job.job_id)
        if ok:
            count += 1
    return count


def _keyboard(chat_id: int, bot_app) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Prev", callback_data="livequeue:prev"),
                InlineKeyboardButton("➡️ Next", callback_data="livequeue:next"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="livequeue:refresh"),
                InlineKeyboardButton("❌ Cancel Semua Job", callback_data="livequeue:cancel_all"),
            ],
        ]
    )


def _display_jobs(bot_app) -> list[Job]:
    active = bot_app.queue.active_jobs()
    queued = bot_app.queue.queued_jobs()
    return active + queued


def _render_live_queue(bot_app, *, chat_id: Optional[int] = None) -> str:
    bot_app._live_queue_spinner_index = (
        bot_app._live_queue_spinner_index + 1
    ) % len(SPINNER_FRAMES)
    spin = SPINNER_FRAMES[bot_app._live_queue_spinner_index]

    jobs = _display_jobs(bot_app)
    active = [j for j in jobs if j.status in ACTIVE_STATUSES]
    queued = [j for j in jobs if j.status == JobStatus.QUEUED.value]

    page = int(bot_app._live_queue_page.get(chat_id, 0)) if chat_id is not None else 0
    max_page = max(0, (len(jobs) - 1) // PAGE_SIZE)
    page = max(0, min(page, max_page))
    if chat_id is not None:
        bot_app._live_queue_page[chat_id] = page
    start = page * PAGE_SIZE
    visible = jobs[start : start + PAGE_SIZE]

    lines: list[str] = []
    lines.append("<b>📊 ZAEIN Task Monitor</b>")
    lines.append(f"<i>Live update {spin}</i>")
    lines.append("")

    if visible:
        for job in visible:
            lines.extend(_render_job(job, spin))
            lines.append("")
    else:
        lines.append("Tidak ada job aktif / antrean.")
        lines.append("")

    lines.append(f"<b>Step:</b> {page + 1}")
    lines.append(f"<b>Halaman:</b> {page + 1}/{max_page + 1}")
    lines.append(f"<b>Total Tugas:</b> {len(jobs)} | Aktif: {len(active)} | Tunggu: {len(queued)}")
    lines.append("")
    lines.extend(_system_lines(bot_app))
    lines.append("")
    lines.append("Cancel semua: /cancel_all")

    return "\n".join(lines)


def _render_job(job: Job, spin: str) -> list[str]:
    pct = int(job.progress or 0)
    bar = _progress_bar(pct)
    status_label = _status_label(job.status)

    job_id = html.escape(job.job_id)
    title = html.escape(_short(job.title, 64))
    progress_text = html.escape(_short(job.progress_text or "", 90))

    lines = [
        "🔐 <b>Nama:</b> <code>Private Task</code>",
        f"├ <b>Status:</b> {status_label} ({pct:.0f}%)",
        f"├ <code>{bar}</code>",
        f"├ <b>Judul:</b> {title}",
        f"├ <b>Ukuran:</b> {_human_size(job.file_size_bytes)}",
        f"├ <b>Engine:</b> {_engine_for(job)}",
        f"├ <b>Mode:</b> #{html.escape(job.type)}",
    ]
    if progress_text:
        lines.append(f"├ <b>Info:</b> <i>{progress_text}</i>")
    lines.append(f"└ ⛔ /cancel_{job_id}")
    return lines


def _status_label(status: str) -> str:
    return {
        JobStatus.QUEUED.value: "Queued",
        JobStatus.DOWNLOADING.value: "Downloading",
        JobStatus.DOWNLOADED.value: "Downloaded",
        JobStatus.UPLOADING.value: "Uploading",
        JobStatus.ENCODING.value: "Processing",
        JobStatus.COMPLETED.value: "Completed",
        JobStatus.FAILED.value: "Failed",
        JobStatus.CANCELLED.value: "Cancelled",
    }.get(status, status)


def _engine_for(job: Job) -> str:
    if job.type == "mirror_gdrive":
        if job.status in {JobStatus.DOWNLOADING.value, JobStatus.DOWNLOADED.value}:
            return "TGIndex + aiohttp"
        return "Google Drive Resumable"
    if "player4me" in job.type:
        return "Player4Me TUS"
    if "bunny" in job.type:
        return "Bunny Stream"
    return "aiohttp / yt-dlp"


def _progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round((percent / 100) * width)
    empty = width - filled
    return ("█" * filled) + ("░" * empty)


def _human_size(value: Optional[int]) -> str:
    if not value:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{value}B"


def _system_lines(bot_app) -> list[str]:
    cpu = "?"
    ram = "?"
    try:
        import psutil  # type: ignore

        cpu = f"{psutil.cpu_percent(interval=None):.1f}%"
        ram = f"{psutil.virtual_memory().percent:.1f}%"
    except Exception:
        pass

    try:
        usage = shutil.disk_usage(bot_app.cfg.paths.download_dir)
        free = _human_size(usage.free)
    except Exception:
        free = "?"

    down = up = "?"
    try:
        import psutil  # type: ignore

        now = time.monotonic()
        net = psutil.net_io_counters()
        last = getattr(bot_app, "_live_queue_net_last", None)
        if last:
            last_t, last_sent, last_recv = last
            dt = max(0.1, now - last_t)
            down = f"{_human_size(int((net.bytes_recv - last_recv) / dt))}/s"
            up = f"{_human_size(int((net.bytes_sent - last_sent) / dt))}/s"
        bot_app._live_queue_net_last = (now, net.bytes_sent, net.bytes_recv)
    except Exception:
        pass

    return [
        "<b>╭─ SYSTEM</b>",
        f"├ 🔴 Cpu  [{_mini_bar(cpu)}] {html.escape(cpu)}",
        f"├ 🟢 Ram  [{_mini_bar(ram)}] {html.escape(ram)}",
        f"├ 🟢 Free [{_mini_bar('0')}] {html.escape(free)}",
        f"├ ⚡ Spd ↓ {html.escape(down)} ↑ {html.escape(up)}",
        f"└ 🌐 Net best-effort",
    ]


def _mini_bar(percent_text: str, width: int = 10) -> str:
    try:
        val = float(str(percent_text).replace("%", ""))
    except Exception:
        val = 0.0
    filled = round((max(0.0, min(100.0, val)) / 100) * width)
    return "■" * filled + "□" * (width - filled)


def _short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _ensure_spinner_task(bot_app, chat_id: int) -> None:
    task = bot_app._live_queue_tasks.get(chat_id)
    if task is not None and not task.done():
        return

    async def loop() -> None:
        while True:
            try:
                if chat_id not in bot_app._live_queue_messages:
                    return
                await asyncio.sleep(2.0)
                await _refresh_live_queue_messages(bot_app, only_chat_id=chat_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("Live queue spinner loop error chat=%s: %s", chat_id, exc)
                await asyncio.sleep(3.0)

    bot_app._live_queue_tasks[chat_id] = asyncio.create_task(
        loop(),
        name=f"live-queue-spinner-{chat_id}",
    )


async def _refresh_live_queue_messages(
    bot_app,
    *,
    force: bool = False,
    only_chat_id: Optional[int] = None,
) -> None:
    messages: dict[int, int] = getattr(bot_app, "_live_queue_messages", {})
    if not messages:
        return

    now = time.monotonic()

    for chat_id, message_id in list(messages.items()):
        if only_chat_id is not None and chat_id != only_chat_id:
            continue

        if not force:
            last_edit = bot_app._live_queue_last_edit.get(chat_id, 0.0)
            if now - last_edit < bot_app._live_queue_min_interval:
                continue

        text = _render_live_queue(bot_app, chat_id=chat_id)
        old = bot_app._live_queue_last_text.get(chat_id)

        if old == text and not force:
            continue

        try:
            await bot_app.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(chat_id, bot_app),
                disable_web_page_preview=True,
            )
            bot_app._live_queue_last_text[chat_id] = text
            bot_app._live_queue_last_edit[chat_id] = time.monotonic()
        except Exception as exc:
            log.debug("Gagal edit live queue chat=%s msg=%s: %s", chat_id, message_id, exc)


def _wrap_queue_status_change(bot_app) -> None:
    old_callback = bot_app.queue._on_status_change

    async def wrapped(job: Job) -> None:
        if old_callback is not None:
            await old_callback(job)
        await _refresh_live_queue_messages(bot_app)

    bot_app.queue._on_status_change = wrapped


def _wrap_publish_bot_commands(bot_app) -> None:
    old_publish = bot_app._publish_bot_commands

    async def wrapped_publish() -> None:
        await old_publish()

        try:
            existing = list(await bot_app.application.bot.get_my_commands())
            by_name = {c.command: c.description for c in existing}
            by_name["queue_live"] = "Antrean live auto-update"
            by_name["cancel_all"] = "Batalkan semua job antrean"

            commands = [BotCommand(cmd, desc) for cmd, desc in by_name.items()]
            await bot_app.application.bot.set_my_commands(commands)
        except Exception as exc:
            log.warning("Gagal menambahkan command live queue ke menu: %s", exc)

    bot_app._publish_bot_commands = wrapped_publish
