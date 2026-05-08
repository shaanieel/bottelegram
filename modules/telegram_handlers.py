"""Telegram bot command handlers.

This module wires every ``/command`` to the underlying queue, downloader,
uploader, and storage helpers.
"""

from __future__ import annotations

import asyncio
import html
import io
import shutil
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Awaitable, Callable

import yaml
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .bunny_uploader import BunnyUploader
from .config_manager import AppConfig
from .downloader import DownloadError, download, is_uploadable_video
from .logger import get_logger
from .queue_manager import Job, JobStatus, JobType, QueueManager
from .storage_manager import (
    clear_directory,
    delete_file,
    disk_stats,
    folder_size,
    human_bytes,
    list_files,
    safe_unlink,
)
from .validators import parse_command_args

log = get_logger(__name__)


# ----- Auth decorator ------------------------------------------------------- #

def admin_only(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg: AppConfig = context.application.bot_data["config"]
        user = update.effective_user
        if user is None or user.id not in cfg.secrets.admin_telegram_ids:
            log.warning(
                "Akses ditolak untuk user_id=%s (@%s)",
                getattr(user, "id", None),
                getattr(user, "username", None),
            )
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Akses ditolak. Bot ini hanya untuk admin."
                )
            return
        return await handler(update, context)

    return wrapper


# ----- Bot wiring ---------------------------------------------------------- #

class BotApp:
    """Glue between :mod:`python-telegram-bot` and the rest of the project."""

    def __init__(
        self,
        cfg: AppConfig,
        application: Application,
    ) -> None:
        self.cfg = cfg
        self.application = application
        self.bunny = BunnyUploader(cfg)
        self.queue = QueueManager(
            cfg,
            worker=self._run_job,
            on_status_change=self._on_job_status_change,
        )
        # Track Telegram message_id used for live progress per job
        self._progress_msg: dict[str, tuple[int, int]] = {}
        self._last_render: dict[str, str] = {}
        self._last_progress_emit: dict[str, float] = {}

        application.bot_data["config"] = cfg
        application.bot_data["queue"] = self.queue
        application.bot_data["bunny"] = self.bunny
        application.bot_data["bot_app"] = self

        self._register_handlers()

    # ---- registration --------------------------------------------------- #

    def _register_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("upload_bunny", self.cmd_upload_bunny))
        app.add_handler(CommandHandler("download_only", self.cmd_download_only))
        app.add_handler(CommandHandler("download", self.cmd_download_only))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("retry", self.cmd_retry))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("list_files", self.cmd_list_files))
        app.add_handler(CommandHandler("delete_file", self.cmd_delete_file))
        app.add_handler(CommandHandler("clear_downloads", self.cmd_clear_downloads))
        app.add_handler(CommandHandler("storage", self.cmd_storage))
        app.add_handler(CommandHandler("health", self.cmd_health))
        app.add_handler(CommandHandler("config", self.cmd_config))
        app.add_handler(CommandHandler("backup_config", self.cmd_backup_config))
        # Fallback for unknown text from non-admins so we always reply gracefully
        app.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))

        app.add_error_handler(self._on_error)

    async def start(self) -> None:
        await self.queue.start()

    # ---- command implementations ---------------------------------------- #

    @staticmethod
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("Unhandled error: %s", context.error)

    async def cmd_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cfg: AppConfig = context.application.bot_data["config"]
        user = update.effective_user
        if user is None or user.id not in cfg.secrets.admin_telegram_ids:
            return  # silently ignore non-admin commands
        if update.effective_message:
            await update.effective_message.reply_text(
                "Command tidak dikenal. Gunakan /help untuk melihat daftar command."
            )

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            f"<b>{html.escape(self.cfg.app.name)}</b>\n\n"
            "Bot automation untuk download video dari banyak sumber lalu upload ke "
            "<b>Bunny Stream</b>.\n\n"
            "Sumber yang didukung: Google Drive, Dropbox, OneDrive, direct link, "
            "dan link yang didukung yt-dlp.\n\n"
            "Ketik /help untuk melihat semua command."
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "<b>Daftar Command</b>\n"
            "/start — info bot\n"
            "/help — bantuan\n"
            "/upload_bunny <i>Judul | URL</i> — download lalu upload ke Bunny Stream\n"
            "/download <i>Judul | URL</i> — download saja (alias /download_only)\n"
            "/download_only <i>Judul | URL</i> — download saja\n"
            "/status <i>VIDEO_ID</i> — cek status video Bunny\n"
            "/queue — lihat antrean\n"
            "/cancel <i>JOB_ID</i> — batalkan job\n"
            "/retry <i>JOB_ID</i> — ulangi job gagal\n"
            "/history — 10 job terakhir\n"
            "/list_files — isi folder downloads\n"
            "/delete_file <i>nama_file</i> — hapus file\n"
            "/clear_downloads — hapus semua file di downloads\n"
            "/storage — info storage\n"
            "/health — health check\n"
            "/config — tampilkan konfigurasi (tanpa secret)\n"
            "/backup_config — export konfigurasi aman"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_upload_bunny(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(update, context, JobType.UPLOAD_BUNNY)

    @admin_only
    async def cmd_download_only(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(update, context, JobType.DOWNLOAD_ONLY)

    async def _enqueue_from_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        job_type: JobType,
    ) -> None:
        msg = update.effective_message
        if not msg:
            return
        raw = (msg.text or "").split(maxsplit=1)
        args = raw[1] if len(raw) > 1 else ""
        title, url = parse_command_args(args)
        if not url:
            await msg.reply_text(
                "Format salah. Gunakan:\n"
                "/upload_bunny Judul Video | https://link\n"
                "atau\n"
                "/download Judul | https://link"
            )
            return

        if job_type == JobType.UPLOAD_BUNNY and not self.cfg.upload_targets.bunny_stream_enabled:
            await msg.reply_text("Upload Bunny dinonaktifkan di config.yaml.")
            return

        if job_type == JobType.UPLOAD_BUNNY and not self.bunny.is_configured():
            await msg.reply_text(
                "Bunny belum dikonfigurasi (BUNNY_API_KEY / library_id kosong)."
            )
            return

        title = title or "Untitled"
        user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None

        job = await self.queue.enqueue(
            job_type=job_type,
            title=title,
            source_url=url,
            chat_id=chat_id,
            user_id=user.id if user else None,
            requested_by=(user.username or user.full_name) if user else None,
        )
        sent = await msg.reply_text(
            f"Job <b>{job.job_id}</b> ditambahkan ke antrean.\n"
            f"Tipe: {job.type}\nJudul: {html.escape(title)}",
            parse_mode=ParseMode.HTML,
        )
        if sent and chat_id is not None:
            self._progress_msg[job.job_id] = (chat_id, sent.message_id)

    @admin_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not context.args:
            await msg.reply_text("Gunakan: /status VIDEO_ID")
            return
        video_id = context.args[0].strip()
        if not self.bunny.is_configured():
            await msg.reply_text("Bunny belum dikonfigurasi.")
            return
        try:
            v = await self.bunny.get_video(video_id)
        except Exception as exc:
            await msg.reply_text(f"Gagal cek status: {exc}")
            return
        text = (
            f"<b>Status Bunny Stream</b>\n"
            f"Judul: {html.escape(v.title or '-')}\n"
            f"Video ID: <code>{html.escape(v.video_id)}</code>\n"
            f"Status: {v.status_text} ({v.status})\n"
            f"Embed: {v.embed_url}\n"
            f"CDN: {self.cfg.bunny.cdn_hostname}"
        )
        await msg.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        active = self.queue.active_jobs()
        queued = self.queue.queued_jobs()
        if not active and not queued:
            await update.effective_message.reply_text("Antrean kosong.")
            return
        lines = ["<b>Antrean</b>"]
        if active:
            lines.append("\n<b>Sedang berjalan</b>")
            for j in active:
                lines.append(_short_job_line(j))
        if queued:
            lines.append("\n<b>Menunggu</b>")
            for j in queued:
                lines.append(_short_job_line(j))
        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not context.args:
            await msg.reply_text("Gunakan: /cancel JOB_ID")
            return
        job_id = context.args[0].strip()
        ok = await self.queue.cancel(job_id)
        if ok:
            await msg.reply_text(f"Permintaan cancel dikirim untuk job {job_id}.")
        else:
            await msg.reply_text(
                f"Job {job_id} tidak bisa dibatalkan (tidak ada / sudah selesai)."
            )

    @admin_only
    async def cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not context.args:
            await msg.reply_text("Gunakan: /retry JOB_ID")
            return
        job_id = context.args[0].strip()
        new_job = await self.queue.retry(job_id)
        if new_job:
            await msg.reply_text(
                f"Job {job_id} di-retry. Job baru: {new_job.job_id}"
            )
        else:
            await msg.reply_text(
                f"Job {job_id} tidak bisa di-retry (tidak gagal / tidak ada)."
            )

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        history = self.queue.history(limit=10)
        if not history:
            await update.effective_message.reply_text("Belum ada riwayat job.")
            return
        lines = ["<b>10 Job Terakhir</b>"]
        for j in history:
            lines.append(_short_job_line(j, with_time=True))
        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_list_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        files = list_files(self.cfg.paths.download_dir)
        if not files:
            await update.effective_message.reply_text("Folder downloads kosong.")
            return
        lines = ["<b>Isi folder downloads</b>"]
        for f in files[:50]:
            lines.append(f"• <code>{html.escape(f.name)}</code> — {human_bytes(f.size_bytes)}")
        if len(files) > 50:
            lines.append(f"…dan {len(files) - 50} file lain")
        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_delete_file(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        msg = update.effective_message
        raw = (msg.text or "").split(maxsplit=1)
        name = raw[1].strip() if len(raw) > 1 else ""
        if not name:
            await msg.reply_text("Gunakan: /delete_file nama_file.mp4")
            return
        try:
            removed = delete_file(self.cfg.paths.download_dir, name)
        except (FileNotFoundError, ValueError, IsADirectoryError) as exc:
            await msg.reply_text(f"Gagal: {exc}")
            return
        await msg.reply_text(f"File dihapus: {removed.name}")

    @admin_only
    async def cmd_clear_downloads(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        n = clear_directory(self.cfg.paths.download_dir)
        await update.effective_message.reply_text(
            f"Folder downloads dibersihkan ({n} entri dihapus)."
        )

    @admin_only
    async def cmd_storage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        s = disk_stats(self.cfg.paths.download_dir)
        used_dl = folder_size(self.cfg.paths.download_dir)
        used_tmp = folder_size(self.cfg.paths.temp_dir)
        text = (
            "<b>Storage</b>\n"
            f"Total: {human_bytes(s.total_bytes)}\n"
            f"Terpakai: {human_bytes(s.used_bytes)} ({s.used_percent:.1f}%)\n"
            f"Sisa: {human_bytes(s.free_bytes)}\n\n"
            f"Folder downloads: {human_bytes(used_dl)}\n"
            f"Folder temp: {human_bytes(used_tmp)}"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        sent = await msg.reply_text("Menjalankan health check…")

        net_ok, net_msg = await asyncio.to_thread(_check_internet)
        bunny_ok, bunny_msg = await self.bunny.health_check()
        ffmpeg_path = shutil.which("ffmpeg")
        py_version = ".".join(str(v) for v in sys.version_info[:3])

        folders = []
        for label, path in (
            ("downloads", self.cfg.paths.download_dir),
            ("temp", self.cfg.paths.temp_dir),
            ("logs", self.cfg.paths.log_dir),
            ("data", self.cfg.paths.data_dir),
        ):
            ok = path.exists() and path.is_dir()
            folders.append(f"{'OK' if ok else 'MISSING'} {label}")

        text = (
            "<b>Health Check</b>\n"
            f"Bot: OK\n"
            f"Python: {py_version}\n"
            f"Internet: {'OK' if net_ok else 'FAIL'} ({net_msg})\n"
            f"Bunny API: {'OK' if bunny_ok else 'FAIL'} ({bunny_msg})\n"
            f"FFmpeg: {'OK (' + ffmpeg_path + ')' if ffmpeg_path else 'tidak ditemukan (opsional)'}\n"
            "Folder:\n  " + "\n  ".join(folders)
        )
        await sent.edit_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        view = self.cfg.safe_view()
        text = "<b>Konfigurasi aktif (tanpa secret)</b>\n<pre>"
        text += html.escape(yaml.safe_dump(view, sort_keys=False, allow_unicode=True))
        text += "</pre>"
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_backup_config(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        view = self.cfg.safe_view()
        view["_note"] = (
            "Backup ini TIDAK berisi token / API key. Isi ulang manual setelah restore."
        )
        view["_admin_telegram_ids_count"] = len(self.cfg.secrets.admin_telegram_ids)
        text = yaml.safe_dump(view, sort_keys=False, allow_unicode=True)
        bio = io.BytesIO(text.encode("utf-8"))
        bio.name = f"config-backup-{int(time.time())}.yaml"
        await update.effective_message.reply_document(
            document=bio,
            filename=bio.name,
            caption="Backup konfigurasi (tanpa secret).",
        )

    # ---- worker (runs the actual job) ----------------------------------- #

    async def _run_job(
        self,
        job: Job,
        cancel_event: asyncio.Event,
        progress: Callable[[str, float | None, str], Awaitable[None]],
    ) -> None:
        # 1) Download
        try:
            result = await download(
                job.source_url,
                job.title,
                self.cfg,
                progress_cb=progress,
                cancel_event=cancel_event,
            )
        except DownloadError as exc:
            raise RuntimeError(str(exc)) from exc

        await self.queue._update(  # type: ignore[attr-defined]
            job,
            file_path=str(result.path),
            file_size_bytes=result.size_bytes,
            status=JobStatus.DOWNLOADED,
            progress=100.0,
            progress_text=f"Download selesai ({human_bytes(result.size_bytes)})",
        )

        # 2) Upload (if requested)
        if job.type == JobType.DOWNLOAD_ONLY.value:
            await self.queue._update(  # type: ignore[attr-defined]
                job,
                status=JobStatus.COMPLETED,
                finished_at=time.time(),
                progress=100.0,
                progress_text="Download selesai (download_only)",
            )
            return

        if not is_uploadable_video(result.path, self.cfg):
            raise RuntimeError(
                f"File {result.path.name} bukan video yang valid untuk Bunny "
                f"(ekstensi tidak didukung)."
            )

        try:
            video = await self.bunny.upload_full(
                title=job.title,
                file_path=result.path,
                progress_cb=progress,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            raise RuntimeError(f"Upload Bunny gagal: {exc}") from exc

        await self.queue._update(  # type: ignore[attr-defined]
            job,
            bunny_video_id=video.video_id,
            bunny_status=video.status_text,
            embed_url=video.embed_url,
            status=JobStatus.COMPLETED,
            finished_at=time.time(),
            progress=100.0,
            progress_text=f"Upload selesai. Status Bunny: {video.status_text}",
        )

        # 3) Auto delete local file if configured
        if self.cfg.app.auto_delete_after_upload:
            safe_unlink(result.path)
            log.info("Auto-deleted local file %s", result.path)

    # ---- live progress notifications ------------------------------------ #

    async def _on_job_status_change(self, job: Job) -> None:
        # 1) edit/replace inline message
        await self._render_progress_message(job)

        # 2) terminal events get a final standalone message
        if job.status == JobStatus.COMPLETED.value:
            await self._send_completion(job)
        elif job.status == JobStatus.FAILED.value:
            await self._send_failure(job)
        elif job.status == JobStatus.CANCELLED.value:
            if job.chat_id:
                await self._safe_send(
                    job.chat_id,
                    f"Job <b>{job.job_id}</b> dibatalkan.",
                )

    async def _render_progress_message(self, job: Job) -> None:
        loc = self._progress_msg.get(job.job_id)
        if not loc:
            return
        chat_id, message_id = loc

        # Throttle edits: don't spam Telegram more than once per ~3 seconds for
        # in-progress jobs. Always render terminal states.
        now = time.time()
        terminal = job.status in (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        last = self._last_progress_emit.get(job.job_id, 0.0)
        if not terminal and now - last < 3.0:
            return

        text = _job_progress_text(job)
        if self._last_render.get(job.job_id) == text:
            return
        self._last_render[job.job_id] = text
        self._last_progress_emit[job.job_id] = now
        try:
            await self.application.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.debug("Edit progress message failed: %s", exc)

    async def _send_completion(self, job: Job) -> None:
        if not job.chat_id:
            return
        if job.type == JobType.UPLOAD_BUNNY.value:
            text = (
                "<b>Upload Bunny Berhasil</b>\n\n"
                f"Judul:\n{html.escape(job.title)}\n\n"
                f"Video ID:\n<code>{html.escape(job.bunny_video_id or '-')}</code>\n\n"
                f"Status:\n{html.escape(job.bunny_status or '-')}\n\n"
                f"Embed URL:\n{job.embed_url or '-'}\n\n"
                f"CDN Hostname:\n{self.cfg.bunny.cdn_hostname}\n\n"
                "Catatan:\nVideo mungkin masih encoding beberapa menit."
            )
        else:
            text = (
                f"<b>Download Selesai</b>\n\n"
                f"Job: <code>{job.job_id}</code>\n"
                f"Judul: {html.escape(job.title)}\n"
                f"File: <code>{html.escape(Path(job.file_path or '').name)}</code>\n"
                f"Ukuran: {human_bytes(job.file_size_bytes or 0)}"
            )
        await self._safe_send(job.chat_id, text)

    async def _send_failure(self, job: Job) -> None:
        if not job.chat_id:
            return
        text = (
            f"<b>Job Gagal</b>\n\n"
            f"Job: <code>{job.job_id}</code>\n"
            f"Tipe: {job.type}\n"
            f"Judul: {html.escape(job.title)}\n"
            f"Error: {html.escape(job.error_message or 'unknown')}\n\n"
            f"Gunakan <code>/retry {job.job_id}</code> untuk mencoba lagi."
        )
        await self._safe_send(job.chat_id, text)

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            log.debug("send_message failed: %s", exc)


# ----- helpers ------------------------------------------------------------- #

def _short_job_line(job: Job, *, with_time: bool = False) -> str:
    title = html.escape(job.title)
    pct = f" {job.progress:.0f}%" if job.progress else ""
    base = (
        f"• <code>{job.job_id}</code> [{job.type}] {job.status}{pct} — {title}"
    )
    if with_time and job.finished_at:
        ts = datetime.fromtimestamp(job.finished_at, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        base += f" ({ts})"
    return base


def _job_progress_text(job: Job) -> str:
    title = html.escape(job.title)
    progress_text = html.escape(job.progress_text or "")
    err = (
        f"\nError: {html.escape(job.error_message)}"
        if job.error_message and job.status == JobStatus.FAILED.value
        else ""
    )
    return (
        f"<b>Job {job.job_id}</b>\n"
        f"Tipe: {job.type}\n"
        f"Judul: {title}\n"
        f"Status: {job.status} ({job.progress:.0f}%)\n"
        f"Detail: {progress_text}{err}"
    )


def _check_internet() -> tuple[bool, str]:
    """Quick TCP+HTTP probe for connectivity."""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=5).close()
    except OSError as exc:
        return False, f"DNS/TCP gagal: {exc}"
    try:
        with urllib.request.urlopen("https://www.google.com", timeout=10) as r:
            return r.status < 500, f"HTTP {r.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
