"""Live queue + quick cancel handlers.

Fitur yang ditambahkan:
- /queue_live
  Menampilkan 1 pesan antrean yang terus di-update otomatis.
- /cancel_<job_id>
  Contoh: /cancel_a5ebc3bb
  Bisa langsung diklik dari pesan live queue.
- /cancel_all
  Membatalkan semua job yang queued / downloading / uploading / encoding.
- Tombol inline "Cancel Semua Job" di /queue_live.

Cara pasang:
1. Simpan file ini ke: modules/live_queue_handlers.py
2. Di bot.py:
   from modules.live_queue_handlers import install_live_queue_handlers

   bot_app = BotApp(cfg, application)
   install_live_queue_handlers(bot_app)
"""

from __future__ import annotations

import asyncio
import html
import time
from typing import Awaitable, Callable, Optional

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


def install_live_queue_handlers(bot_app) -> None:
    """Install live queue handlers ke BotApp tanpa mengubah telegram_handlers.py.

    Handler dipasang di group -1 supaya diproses sebelum fallback unknown command
    yang sudah ada di telegram_handlers.py.
    """
    if getattr(bot_app, "_live_queue_handlers_installed", False):
        return

    bot_app._live_queue_handlers_installed = True
    bot_app._live_queue_messages = {}          # chat_id -> message_id
    bot_app._live_queue_last_text = {}         # chat_id -> last rendered html
    bot_app._live_queue_last_edit = {}         # chat_id -> monotonic timestamp
    bot_app._live_queue_spinner_index = 0
    bot_app._live_queue_tasks = {}             # chat_id -> asyncio.Task
    bot_app._live_queue_min_interval = 1.5     # anti flood edit Telegram

    app = bot_app.application

    # group=-1 supaya tidak ketahan MessageHandler(filters.COMMAND, cmd_unknown)
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
    if user is None:
        return False
    return user.id in bot_app.cfg.secrets.admin_telegram_ids


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
        text = _render_live_queue(bot_app)

        sent = await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(),
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

        action = query.data.split(":", 1)[1]

        if action == "cancel_all":
            count = await _cancel_all_jobs(bot_app)
            await query.answer(f"{count} job dibatalkan.", show_alert=True)
            await _refresh_live_queue_messages(bot_app, force=True)
            return

        if action == "refresh":
            await query.answer("Refresh antrean.")
            await _refresh_live_queue_messages(bot_app, force=True)
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


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="livequeue:refresh"),
                InlineKeyboardButton("❌ Cancel Semua Job", callback_data="livequeue:cancel_all"),
            ]
        ]
    )


def _render_live_queue(bot_app) -> str:
    bot_app._live_queue_spinner_index = (
        bot_app._live_queue_spinner_index + 1
    ) % len(SPINNER_FRAMES)
    spin = SPINNER_FRAMES[bot_app._live_queue_spinner_index]

    active = bot_app.queue.active_jobs()
    queued = bot_app.queue.queued_jobs()

    lines: list[str] = []
    lines.append("<b>Antrean Live</b>")
    lines.append(f"<i>Auto update aktif {spin}</i>")
    lines.append("")

    if active:
        lines.append("<b>Sedang berjalan</b>")
        for job in active[:6]:
            lines.extend(_render_active_job(job, spin))
    else:
        lines.append("<b>Sedang berjalan</b>")
        lines.append("Tidak ada job aktif.")

    lines.append("")
    lines.append("<b>Menunggu</b>")

    if queued:
        for job in queued[:30]:
            lines.append(
                "• "
                f"<code>{html.escape(job.job_id)}</code> "
                f"[{html.escape(job.type)}] "
                f"{html.escape(_short(job.title, 72))}"
            )
        if len(queued) > 30:
            lines.append(f"… dan {len(queued) - 30} job lainnya.")
    else:
        lines.append("Tidak ada antrean.")

    lines.append("")
    lines.append(f"Total aktif: <b>{len(active)}</b> | Menunggu: <b>{len(queued)}</b>")
    lines.append("Cancel semua: /cancel_all")

    return "\n".join(lines)


def _render_active_job(job: Job, spin: str) -> list[str]:
    pct = int(job.progress or 0)
    bar = _progress_bar(pct)

    status = html.escape(job.status)
    job_id = html.escape(job.job_id)
    title = html.escape(_short(job.title, 78))
    progress_text = html.escape(_short(job.progress_text or "", 90))

    lines = [
        f"{spin} <code>{job_id}</code> [{html.escape(job.type)}] {status} {pct}%",
        f"{bar}",
        f"🎬 {title}",
    ]

    if progress_text:
        lines.append(f"   <i>{progress_text}</i>")

    # Ini sengaja format command tanpa spasi agar bisa diklik langsung di Telegram.
    lines.append(f"   Batalkan: /cancel_{job_id}")
    return lines


def _progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round((percent / 100) * width)
    empty = width - filled
    return "   " + ("█" * filled) + ("░" * empty) + f" {percent}%"


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
                # Kalau pesan live masih ada, tetap update spinner.
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

        text = _render_live_queue(bot_app)
        old = bot_app._live_queue_last_text.get(chat_id)

        # Walaupun spinner berubah, kadang text bisa sama kalau belum masuk render.
        if old == text and not force:
            continue

        try:
            await bot_app.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(),
                disable_web_page_preview=True,
            )
            bot_app._live_queue_last_text[chat_id] = text
            bot_app._live_queue_last_edit[chat_id] = time.monotonic()
        except Exception as exc:
            # Kalau message sudah dihapus / terlalu lama / tidak bisa diedit,
            # jangan matikan bot. Cukup log debug.
            log.debug("Gagal edit live queue chat=%s msg=%s: %s", chat_id, message_id, exc)


def _wrap_queue_status_change(bot_app) -> None:
    old_callback = bot_app.queue._on_status_change

    async def wrapped(job: Job) -> None:
        if old_callback is not None:
            await old_callback(job)
        await _refresh_live_queue_messages(bot_app)

    bot_app.queue._on_status_change = wrapped


def _wrap_publish_bot_commands(bot_app) -> None:
    """Tambahkan command baru ke menu Telegram tanpa edit telegram_handlers.py."""
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
