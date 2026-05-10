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
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Awaitable, Callable

import yaml
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .bunny_uploader import BunnyUploader
from .config_manager import AppConfig
from .downloader import DownloadError, download, is_uploadable_video
from .gdrive_api import GDriveAPIClient, GDriveAPIError
from .gdrive_uploader import GDriveUploader
from .logger import get_logger
from .player4me_uploader import (
    Player4MeError,
    Player4MeUploader,
    Player4MeUploadResult,
)
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
from .subtitle_extractor import (
    ExtractedSubtitle,
    SubtitleExtractError,
    detect_language_from_filename,
    extract_embedded_subtitles,
)
from .telegram_handlers_gdrive import (
    register_gdrive_handlers,
    run_mirror_gdrive_full,
)
from .validators import (
    classify_link,
    is_url,
    is_video_extension,
    parse_command_args,
    safe_filename,
)

log = get_logger(__name__)


# ----- Auth decorator ------------------------------------------------------- #

def admin_only(handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Reject any update whose sender is not in ``ADMIN_TELEGRAM_ID``.

    Works for both standalone functions ``handler(update, context)`` and bound
    methods ``handler(self, update, context)`` — the last two positional args
    are always the ``Update`` and ``ContextTypes.DEFAULT_TYPE`` instances.
    """

    @wraps(handler)
    async def wrapper(*args, **kwargs) -> None:
        if len(args) < 2:
            return await handler(*args, **kwargs)
        update: Update = args[-2]
        context: ContextTypes.DEFAULT_TYPE = args[-1]
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
        return await handler(*args, **kwargs)

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
        self.player4me = Player4MeUploader(cfg)
        self.gdrive_upload = GDriveUploader(cfg)
        self.queue = QueueManager(
            cfg,
            worker=self._run_job,
            on_status_change=self._on_job_status_change,
        )
        # Track Telegram message_id used for live progress per job
        self._progress_msg: dict[str, tuple[int, int]] = {}
        self._last_render: dict[str, str] = {}
        self._last_progress_emit: dict[str, float] = {}
        # Pending /m and /upload_player4me requests waiting for the user to
        # pick a target / mode. Mapping: pending_id -> (chat_id, user_id,
        # title, url).
        self._pending_mirror: dict[
            str, tuple[int, int, str, str]
        ] = {}

        application.bot_data["config"] = cfg
        application.bot_data["queue"] = self.queue
        application.bot_data["bunny"] = self.bunny
        application.bot_data["player4me"] = self.player4me
        application.bot_data["gdrive_upload"] = self.gdrive_upload
        application.bot_data["bot_app"] = self

        self._register_handlers()

    # ---- registration --------------------------------------------------- #

    def _register_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        # Short mirror command — download then prompt for upload target.
        app.add_handler(CommandHandler("m", self.cmd_mirror))
        app.add_handler(CommandHandler("mirror", self.cmd_mirror))
        app.add_handler(CommandHandler("upload_bunny", self.cmd_upload_bunny))
        app.add_handler(
            CommandHandler("upload_player4me", self.cmd_upload_player4me)
        )
        app.add_handler(
            CommandHandler(
                "upload_player4me_subs", self.cmd_upload_player4me_subs
            )
        )
        app.add_handler(
            CommandHandler(
                "upload_player4me_folder", self.cmd_upload_player4me_folder
            )
        )
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
        # Inline-keyboard callbacks for /m flow
        app.add_handler(
            CallbackQueryHandler(self.cb_mirror_choice, pattern=r"^mirror:")
        )
        # /gdrive_list + /mirror_gdrive (Telegram Index → Drive)
        register_gdrive_handlers(app, self)
        # Fallback for unknown text from non-admins so we always reply gracefully
        app.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))

        app.add_error_handler(self._on_error)

    async def start(self) -> None:
        await self.queue.start()
        await self._publish_bot_commands()

    async def _publish_bot_commands(self) -> None:
        """Tell Telegram which commands to autocomplete when user types ``/``."""
        commands = [
            BotCommand("start", "Info bot dan panduan singkat"),
            BotCommand("help", "Daftar semua command"),
            BotCommand("m", "Mirror: download lalu pilih target upload"),
            BotCommand(
                "upload_bunny",
                "Download + upload ke Bunny Stream",
            ),
            BotCommand(
                "upload_player4me",
                "Player4Me: pilih mode (video saja / sub embed / sub folder)",
            ),
            BotCommand(
                "upload_player4me_subs",
                "Player4Me + auto-extract sub embed (mkv/mp4)",
            ),
            BotCommand(
                "upload_player4me_folder",
                "Player4Me dari folder Drive + sub file sidecar",
            ),
            BotCommand("download", "Download saja, tanpa upload"),
            BotCommand(
                "gdrive_list",
                "Preview file di Telegram Index (filter keyword)",
            ),
            BotCommand(
                "mirror_gdrive",
                "Mirror Telegram Index → Google Drive (bulk)",
            ),
            BotCommand("status", "Cek status video Bunny"),
            BotCommand("queue", "Lihat antrean job"),
            BotCommand("cancel", "Batalkan job (JOB_ID)"),
            BotCommand("retry", "Ulangi job gagal (JOB_ID)"),
            BotCommand("history", "10 job terakhir"),
            BotCommand("list_files", "Isi folder downloads"),
            BotCommand("delete_file", "Hapus file di downloads"),
            BotCommand("clear_downloads", "Hapus semua file di downloads"),
            BotCommand("storage", "Info storage"),
            BotCommand("health", "Health check"),
            BotCommand("config", "Tampilkan konfigurasi (tanpa secret)"),
            BotCommand("backup_config", "Export konfigurasi aman"),
        ]
        try:
            await self.application.bot.set_my_commands(commands)
            log.info("Bot command menu di-publish (%d entri)", len(commands))
        except Exception as exc:
            log.warning("Gagal set_my_commands: %s", exc)

    # ---- command implementations ---------------------------------------- #

    @staticmethod
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # ``Conflict`` is raised once per long-poll cycle when another bot
        # process is already calling ``getUpdates`` with the same token.
        # The traceback adds no value (it's the same every time) so we
        # collapse it to a single throttled WARNING line. Real bugs still
        # get the full stack trace via ``log.exception``.
        try:
            from telegram.error import Conflict, NetworkError, TimedOut
        except ImportError:
            Conflict = NetworkError = TimedOut = ()  # type: ignore[assignment]
        err = context.error
        if isinstance(err, Conflict):
            now = time.monotonic()
            last = getattr(BotApp, "_last_conflict_log_at", 0.0)
            if now - last > 60:
                BotApp._last_conflict_log_at = now
                log.warning(
                    "Telegram getUpdates conflict: instance bot lain pakai "
                    "token yang sama. Pastikan hanya 1 'python bot.py' jalan."
                )
            return
        if isinstance(err, (NetworkError, TimedOut)):
            log.warning("Telegram network hiccup: %s", err)
            return
        log.exception("Unhandled error: %s", err)

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
            "/m <i>URL</i> — mirror: download lalu pilih tombol upload (Bunny / Player4Me)\n"
            "/upload_bunny <i>[Judul |] URL</i> — download + upload ke Bunny Stream\n"
            "/upload_player4me <i>[Judul |] URL</i> — Player4Me: tampilkan tombol pilih mode upload (video saja / sub embed / sub folder)\n"
            "/upload_player4me_subs <i>[Judul |] URL</i> — Player4Me + auto-extract sub embed di video (mkv/mp4) [skip picker]\n"
            "/upload_player4me_folder <i>[Judul |] URL_FOLDER_DRIVE</i> — Player4Me dari folder Drive (video + file sub terpisah) [skip picker]\n"
            "/download <i>[Judul |] URL</i> — download saja (alias /download_only)\n"
            "/download_only <i>[Judul |] URL</i> — download saja\n"
            "/gdrive_list <i>URL_INDEX [| keyword]</i> — preview file di Telegram Index, optional filter keyword (contoh: vmx)\n"
            "/mirror_gdrive <i>URL_INDEX [| keyword] [| folder_id]</i> — bulk download dari Telegram Index lalu upload ke Google Drive\n"
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
            "/backup_config — export konfigurasi aman\n\n"
            "<i>Kalau Judul tidak diberikan, judul otomatis diambil dari nama file di sumber.</i>"
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_upload_bunny(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(update, context, JobType.UPLOAD_BUNNY)

    @admin_only
    async def cmd_upload_player4me(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """``/upload_player4me URL`` — show a button picker so the user can
        choose between *video saja*, *sub embed (auto-extract)*, atau *sub
        file (folder Drive)*. Untuk skip picker, pakai
        ``/upload_player4me_subs`` atau ``/upload_player4me_folder``.
        """
        await self._show_player4me_picker(update, context)

    @admin_only
    async def cmd_upload_player4me_subs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(
            update, context, JobType.UPLOAD_PLAYER4ME_SUBS
        )

    @admin_only
    async def cmd_upload_player4me_folder(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(
            update, context, JobType.UPLOAD_PLAYER4ME_FOLDER
        )

    @admin_only
    async def cmd_download_only(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await self._enqueue_from_command(update, context, JobType.DOWNLOAD_ONLY)

    @admin_only
    async def cmd_mirror(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """``/m URL`` — register a pending mirror and prompt for upload target."""
        msg = update.effective_message
        if not msg:
            return
        raw = (msg.text or "").split(maxsplit=1)
        args = raw[1].strip() if len(raw) > 1 else ""
        if not args:
            await msg.reply_text(
                "Format: /m URL\nContoh:\n/m https://drive.google.com/file/d/FILE_ID/view"
            )
            return

        # Accept either a bare URL or 'Title | URL'.
        title, url = parse_command_args(args)
        if not url:
            url = args.strip()
            title = ""
        if not is_url(url):
            await msg.reply_text(
                "URL tidak valid. Pastikan diawali http:// atau https://"
            )
            return

        # Auto-detect title from Drive metadata when user didn't supply one.
        title = (title or "").strip()
        if not title:
            title = await self._infer_title(url)

        chat_id = update.effective_chat.id if update.effective_chat else 0
        user = update.effective_user
        user_id = user.id if user else 0

        # Detect Drive folder URL so we render the right tombol set.
        try:
            info = classify_link(
                url,
                allow_google_drive=self.cfg.download.allow_google_drive,
                allow_direct=self.cfg.download.allow_direct_link,
                allow_ytdlp=self.cfg.download.allow_ytdlp,
            )
        except ValueError:
            info = None
        is_folder = bool(info and info.kind == "google_drive_folder")

        # 8 hex chars is plenty for callback uniqueness within this process.
        pending_id = uuid.uuid4().hex[:8]
        self._pending_mirror[pending_id] = (chat_id, user_id, title, url)

        kind_hint = "FOLDER Drive" if is_folder else "file"
        text = (
            "<b>Tugas baru — pilih target upload</b>\n"
            f"Judul: {html.escape(title)}\n"
            f"Tipe: {kind_hint}\n"
            f"URL: <code>{html.escape(url)}</code>\n\n"
            "Klik tombol di bawah untuk memilih kemana file di-upload."
        )
        keyboard = self._mirror_keyboard(pending_id, is_folder=is_folder)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def _infer_title(self, url: str) -> str:
        """Derive a sensible title from the source URL (Drive metadata first)."""
        try:
            info = classify_link(
                url,
                allow_google_drive=self.cfg.download.allow_google_drive,
                allow_direct=self.cfg.download.allow_direct_link,
                allow_ytdlp=self.cfg.download.allow_ytdlp,
            )
        except ValueError:
            return "Untitled"

        if info.kind == "google_drive" and info.file_id:
            client = GDriveAPIClient(self.cfg)
            if client.is_configured():
                try:
                    meta = await client.get_metadata(info.file_id)
                    name = str(meta.get("name") or "").strip()
                    if name:
                        # Strip extension so the queued title is clean.
                        return Path(safe_filename(name)).stem or name
                except Exception as exc:
                    log.debug("infer_title gagal ambil meta Drive: %s", exc)

        if info.kind == "google_drive_folder" and info.folder_id:
            client = GDriveAPIClient(self.cfg)
            if client.is_configured() and (
                client.has_oauth_token() or client.has_service_account()
            ):
                try:
                    meta = await client.get_metadata(info.folder_id)
                    name = str(meta.get("name") or "").strip()
                    if name:
                        return safe_filename(name)
                except Exception as exc:
                    log.debug("infer_title gagal ambil meta folder Drive: %s", exc)

        # Fallback: filename portion of the URL path.
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(url).path or "")
        candidate = Path(path).name
        if candidate:
            stem = Path(safe_filename(candidate)).stem
            if stem:
                return stem
        return "Untitled"

    async def _show_player4me_picker(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Tampilkan tombol pilihan mode Player4Me (video saja / embed / folder).

        Logika sama seperti ``cmd_mirror`` tapi keyboard hanya berisi opsi
        Player4Me. Dipakai oleh ``/upload_player4me URL`` supaya user selalu
        diminta memilih 1 dari 3 mode (alih-alih langsung upload tanpa
        ekstraksi subtitle).
        """
        msg = update.effective_message
        if not msg:
            return
        raw = (msg.text or "").split(maxsplit=1)
        args = raw[1].strip() if len(raw) > 1 else ""
        if not args:
            await msg.reply_text(
                "Format: /upload_player4me [Judul |] URL\n"
                "Contoh:\n"
                "/upload_player4me https://drive.google.com/file/d/FILE_ID/view\n"
                "/upload_player4me Judul Film | https://drive.google.com/drive/folders/FOLDER_ID"
            )
            return

        title, url = parse_command_args(args)
        if not url:
            url = args.strip()
            title = ""
        if not is_url(url):
            await msg.reply_text(
                "URL tidak valid. Pastikan diawali http:// atau https://"
            )
            return

        if not self.cfg.upload_targets.player4me_enabled:
            await msg.reply_text(
                "Upload Player4Me dinonaktifkan di config.yaml."
            )
            return
        if not self.player4me.is_configured():
            await msg.reply_text(
                "Player4Me belum dikonfigurasi (PLAYER4ME_API_TOKEN kosong)."
            )
            return

        title = (title or "").strip()
        if not title:
            title = await self._infer_title(url)

        chat_id = update.effective_chat.id if update.effective_chat else 0
        user = update.effective_user
        user_id = user.id if user else 0

        try:
            info = classify_link(
                url,
                allow_google_drive=self.cfg.download.allow_google_drive,
                allow_direct=self.cfg.download.allow_direct_link,
                allow_ytdlp=self.cfg.download.allow_ytdlp,
            )
        except ValueError:
            info = None
        is_folder = bool(info and info.kind == "google_drive_folder")

        pending_id = uuid.uuid4().hex[:8]
        self._pending_mirror[pending_id] = (chat_id, user_id, title, url)

        kind_hint = "FOLDER Drive" if is_folder else "file"
        text = (
            "<b>Upload Player4Me \u2014 pilih mode</b>\n"
            f"Judul: {html.escape(title)}\n"
            f"Tipe: {kind_hint}\n"
            f"URL: <code>{html.escape(url)}</code>\n\n"
            "Pilih salah satu mode di bawah ini:"
        )
        keyboard = self._player4me_keyboard(pending_id, is_folder=is_folder)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    def _player4me_keyboard(
        self, pending_id: str, *, is_folder: bool = False
    ) -> InlineKeyboardMarkup:
        """Keyboard untuk /upload_player4me — tampilkan 3 mode (atau hanya
        mode folder kalau URL adalah folder Drive)."""
        rows: list[list[InlineKeyboardButton]] = []
        if is_folder:
            # Folder URL → hanya mode "sub file (folder)" yang masuk akal.
            rows.append(
                [
                    InlineKeyboardButton(
                        "\U0001F4C1 Player4Me + sub file (folder Drive)",
                        callback_data=f"mirror:p4m_folder:{pending_id}",
                    )
                ]
            )
        else:
            # File tunggal → tampilkan 2 pilihan utama subtitle (embed vs
            # tanpa subtitle), plus 1 tombol untuk mode folder kalau user
            # ternyata salah masukkan URL (yang akan gagal di runtime tapi
            # paling tidak terlihat sebagai opsi).
            rows.append(
                [
                    InlineKeyboardButton(
                        "\U0001F3AC Auto-extract sub embed (mkv/mp4)",
                        callback_data=f"mirror:p4m_subs:{pending_id}",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u25B6 Video saja (tanpa subtitle)",
                        callback_data=f"mirror:player4me:{pending_id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "\u2715 Batal",
                    callback_data=f"mirror:cancel:{pending_id}",
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _mirror_keyboard(
        self, pending_id: str, *, is_folder: bool = False
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        p4m_ok = (
            self.cfg.upload_targets.player4me_enabled
            and self.player4me.is_configured()
        )
        bunny_ok = (
            self.cfg.upload_targets.bunny_stream_enabled
            and self.bunny.is_configured()
        )
        if is_folder:
            # A folder URL only makes sense for the "Player4Me + sub file"
            # variant (sidecar subtitles next to the video file). Bunny / TUS
            # plain don't accept a folder.
            if p4m_ok:
                rows.append(
                    [
                        InlineKeyboardButton(
                            "\u25B6 Player4Me + sub file (folder)",
                            callback_data=f"mirror:p4m_folder:{pending_id}",
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u2715 Batal",
                        callback_data=f"mirror:cancel:{pending_id}",
                    )
                ]
            )
            return InlineKeyboardMarkup(rows)

        first_row: list[InlineKeyboardButton] = []
        if bunny_ok:
            first_row.append(
                InlineKeyboardButton(
                    "\u25B6 Bunny Stream",
                    callback_data=f"mirror:bunny:{pending_id}",
                )
            )
        if p4m_ok:
            first_row.append(
                InlineKeyboardButton(
                    "\u25B6 Player4Me",
                    callback_data=f"mirror:player4me:{pending_id}",
                )
            )
        if first_row:
            rows.append(first_row)
        if p4m_ok:
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u25B6 Player4Me + sub embed (mkv)",
                        callback_data=f"mirror:p4m_subs:{pending_id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "\u2B07 Download saja",
                    callback_data=f"mirror:download:{pending_id}",
                ),
                InlineKeyboardButton(
                    "\u2715 Batal",
                    callback_data=f"mirror:cancel:{pending_id}",
                ),
            ]
        )
        return InlineKeyboardMarkup(rows)

    async def cb_mirror_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle clicks on the /m inline keyboard."""
        query = update.callback_query
        if query is None or not query.data:
            return

        # Authorize the click via the same admin gate as command handlers.
        cfg: AppConfig = context.application.bot_data["config"]
        clicker = update.effective_user
        if clicker is None or clicker.id not in cfg.secrets.admin_telegram_ids:
            await query.answer("Akses ditolak.", show_alert=True)
            return

        try:
            _, action, pending_id = query.data.split(":", 2)
        except ValueError:
            await query.answer("Callback tidak dikenali.", show_alert=True)
            return

        pending = self._pending_mirror.pop(pending_id, None)
        if pending is None:
            await query.answer(
                "Pilihan kadaluarsa. Kirim ulang /m URL.", show_alert=True
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        chat_id, user_id, title, url = pending

        if action == "cancel":
            await query.answer("Dibatalkan.")
            try:
                await query.edit_message_text(
                    f"<b>Mirror dibatalkan</b>\nJudul: {html.escape(title)}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        if action == "bunny":
            job_type = JobType.UPLOAD_BUNNY
            target_label = "Bunny Stream"
            if not self.bunny.is_configured():
                await query.answer(
                    "Bunny belum dikonfigurasi.", show_alert=True
                )
                return
        elif action == "player4me":
            job_type = JobType.UPLOAD_PLAYER4ME
            target_label = "Player4Me"
            if not self.player4me.is_configured():
                await query.answer(
                    "Player4Me belum dikonfigurasi.", show_alert=True
                )
                return
        elif action == "p4m_subs":
            job_type = JobType.UPLOAD_PLAYER4ME_SUBS
            target_label = "Player4Me + sub embed"
            if not self.player4me.is_configured():
                await query.answer(
                    "Player4Me belum dikonfigurasi.", show_alert=True
                )
                return
        elif action == "p4m_folder":
            job_type = JobType.UPLOAD_PLAYER4ME_FOLDER
            target_label = "Player4Me + sub file (folder)"
            if not self.player4me.is_configured():
                await query.answer(
                    "Player4Me belum dikonfigurasi.", show_alert=True
                )
                return
        elif action == "download":
            job_type = JobType.DOWNLOAD_ONLY
            target_label = "Download saja"
        else:
            await query.answer("Aksi tidak dikenali.", show_alert=True)
            return

        await query.answer(f"Mulai: {target_label}")

        job = await self.queue.enqueue(
            job_type=job_type,
            title=title,
            source_url=url,
            chat_id=chat_id,
            user_id=user_id,
            requested_by=(clicker.username or clicker.full_name)
            if clicker
            else None,
        )

        new_text = (
            f"<b>Job {job.job_id} ditambahkan</b>\n"
            f"Target: {target_label}\n"
            f"Judul: {html.escape(title)}\n"
            f"URL: <code>{html.escape(url)}</code>"
        )
        try:
            await query.edit_message_text(
                new_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if query.message:
                self._progress_msg[job.job_id] = (
                    query.message.chat_id,
                    query.message.message_id,
                )
        except Exception as exc:
            log.debug("edit callback message failed: %s", exc)

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
        if not url and is_url(args.strip()):
            url = args.strip()
            title = ""
        if not url:
            await msg.reply_text(
                "Format salah. Gunakan:\n"
                "/upload_bunny Judul | https://link\n"
                "/upload_player4me Judul | https://link\n"
                "/download Judul | https://link\n\n"
                "Judul opsional — kalau dikosongkan akan diambil dari nama file di sumber."
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

        p4m_jobs = {
            JobType.UPLOAD_PLAYER4ME,
            JobType.UPLOAD_PLAYER4ME_SUBS,
            JobType.UPLOAD_PLAYER4ME_FOLDER,
        }
        if job_type in p4m_jobs and not self.cfg.upload_targets.player4me_enabled:
            await msg.reply_text(
                "Upload Player4Me dinonaktifkan di config.yaml."
            )
            return
        if job_type in p4m_jobs and not self.player4me.is_configured():
            await msg.reply_text(
                "Player4Me belum dikonfigurasi (PLAYER4ME_API_TOKEN kosong)."
            )
            return

        # The folder flow only accepts a Drive folder URL because we need to
        # enumerate the contents via the Drive API; reject anything else early
        # rather than silently downloading a single file.
        if job_type == JobType.UPLOAD_PLAYER4ME_FOLDER:
            try:
                info = classify_link(
                    url,
                    allow_google_drive=self.cfg.download.allow_google_drive,
                    allow_direct=self.cfg.download.allow_direct_link,
                    allow_ytdlp=self.cfg.download.allow_ytdlp,
                )
            except ValueError:
                info = None
            if not info or info.kind != "google_drive_folder":
                await msg.reply_text(
                    "Untuk /upload_player4me_folder, URL harus link folder "
                    "Google Drive (drive.google.com/drive/folders/...)."
                )
                return

        title = (title or "").strip()
        if not title:
            title = await self._infer_title(url)
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
        if self.player4me.is_configured():
            p4m_ok, p4m_msg = await self.player4me.health_check()
            p4m_line = (
                f"Player4Me API: {'OK' if p4m_ok else 'FAIL'} ({p4m_msg})"
            )
        else:
            p4m_line = "Player4Me API: tidak dikonfigurasi"
        gdrive_client = GDriveAPIClient(self.cfg)
        if gdrive_client.is_configured():
            gdrive_ok, gdrive_msg = await gdrive_client.health_check()
            gdrive_line = (
                f"GDrive API: {'OK' if gdrive_ok else 'FAIL'} "
                f"[{gdrive_client.auth_mode()}] ({gdrive_msg})"
            )
        else:
            gdrive_line = "GDrive API: tidak dikonfigurasi (fallback ke gdown)"
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
            f"{p4m_line}\n"
            f"{gdrive_line}\n"
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
        # The folder flow doesn't go through the standard ``download()`` path
        # (which expects a single file URL); it has its own download+upload
        # pipeline that pulls every file in the Drive folder one-by-one.
        if job.type == JobType.UPLOAD_PLAYER4ME_FOLDER.value:
            await self._run_upload_player4me_folder(
                job, progress, cancel_event
            )
            return

        # Telegram Index mirror also bypasses the generic downloader because
        # the index hosts require an authenticated session cookie that the
        # generic ``download()`` path doesn't know about. The full pipeline
        # (login + download + upload-to-Drive) lives in ``telegram_handlers_gdrive``.
        if job.type == JobType.MIRROR_GDRIVE.value:
            await run_mirror_gdrive_full(self, job, progress, cancel_event)
            return

        # 1) Download (single file)
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
                f"File {result.path.name} bukan video yang valid "
                f"(ekstensi tidak didukung)."
            )

        if job.type == JobType.UPLOAD_BUNNY.value:
            await self._run_upload_bunny(
                job, result.path, progress, cancel_event
            )
        elif job.type == JobType.UPLOAD_PLAYER4ME.value:
            await self._run_upload_player4me(
                job, result.path, progress, cancel_event
            )
        elif job.type == JobType.UPLOAD_PLAYER4ME_SUBS.value:
            await self._run_upload_player4me_with_embedded_subs(
                job, result.path, progress, cancel_event
            )
        else:
            raise RuntimeError(f"Tipe job tidak dikenali: {job.type}")

        # 3) Auto delete local file if configured
        if self.cfg.app.auto_delete_after_upload:
            safe_unlink(result.path)
            log.info("Auto-deleted local file %s", result.path)

    async def _run_upload_bunny(
        self,
        job: Job,
        file_path: Path,
        progress: Callable[[str, float | None, str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> None:
        try:
            video = await self.bunny.upload_full(
                title=job.title,
                file_path=file_path,
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

    async def _run_upload_player4me(
        self,
        job: Job,
        file_path: Path,
        progress: Callable[[str, float | None, str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> None:
        folder_id = (
            self.cfg.upload_targets.player4me_default_folder_id or None
        )
        try:
            res = await self.player4me.upload_local_file(
                file_path,
                title=job.title,
                folder_id=folder_id,
                progress_cb=progress,
                cancel_event=cancel_event,
            )
        except Player4MeError as exc:
            raise RuntimeError(f"Upload Player4Me gagal: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Upload Player4Me gagal: {exc}") from exc

        await self.queue._update(  # type: ignore[attr-defined]
            job,
            player4me_task_id=res.task_id,
            player4me_video_id=res.primary_video_id,
            player4me_status=res.status,
            player4me_engine=res.engine,
            status=JobStatus.COMPLETED,
            finished_at=time.time(),
            progress=100.0,
            progress_text=(
                f"Upload selesai. Player4Me ({res.engine}): {res.status}"
            ),
        )

    # ---- player4me + embedded subtitle extraction (Flow A) ------------- #

    async def _run_upload_player4me_with_embedded_subs(
        self,
        job: Job,
        file_path: Path,
        progress: Callable[[str, float | None, str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> None:
        """Flow A: extract subs from a single mkv/mp4, upload video + subs.

        The video file has already been downloaded into ``file_path`` by the
        common download stage. We:
          1. Snapshot existing video IDs on Player4Me (used to identify the
             new video after TUS upload, which doesn't return videoId).
          2. Probe the file for embedded subtitle streams and extract every
             text-based one (subrip / ass / vtt / mov_text) to a temp dir.
          3. TUS-upload the video.
          4. List ``/video/manage`` and pick the new entry by name (or by
             diff against the snapshot). That gives us the videoId.
          5. PUT each extracted subtitle to ``/video/{id}/subtitle``.
          6. Clean up the per-job temp dir.
        """
        default_lang = (
            self.cfg.upload_targets.player4me_default_subtitle_language
        )
        folder_id = (
            self.cfg.upload_targets.player4me_default_folder_id or None
        )

        await progress(
            "uploading", 0.0, "Snapshot daftar video Player4Me sebelum upload\u2026"
        )
        try:
            existing_ids = await self.player4me.snapshot_video_ids(
                folder_id=folder_id
            )
        except Player4MeError as exc:
            log.warning("snapshot video gagal (%s) \u2014 lanjut tanpa snapshot", exc)
            existing_ids = set()

        sub_dir = self.cfg.paths.temp_dir / f"subs-{job.job_id}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[ExtractedSubtitle] = []
        try:
            await progress(
                "uploading",
                0.0,
                f"Probe subtitle embed di {file_path.name}\u2026",
            )
            try:
                extracted = await extract_embedded_subtitles(
                    file_path,
                    sub_dir,
                    default_language=default_lang,
                    base_name=Path(safe_filename(job.title)).stem or job.title,
                )
            except SubtitleExtractError as exc:
                # ``ffmpeg`` missing or container probe failed \u2014 don't fail
                # the whole upload, just continue without subs.
                log.warning(
                    "Extract subtitle gagal untuk %s: %s", file_path, exc
                )
                extracted = []

            if extracted:
                summary = ", ".join(
                    f"{s.language}({s.codec})" for s in extracted
                )
                await progress(
                    "uploading",
                    0.0,
                    f"Subtitle ditemukan ({len(extracted)}): {summary}",
                )
            else:
                await progress(
                    "uploading",
                    0.0,
                    "Tidak ada subtitle text yang bisa di-extract \u2014 lanjut upload video saja.",
                )

            # 3) Upload video via TUS.
            try:
                upload_res: Player4MeUploadResult = (
                    await self.player4me.upload_local_file(
                        file_path,
                        title=job.title,
                        folder_id=folder_id,
                        progress_cb=progress,
                        cancel_event=cancel_event,
                    )
                )
            except Player4MeError as exc:
                raise RuntimeError(
                    f"Upload Player4Me (TUS) gagal: {exc}"
                ) from exc

            video_id = upload_res.primary_video_id
            # If TUS didn't surface a videoId, recover by listing recent uploads.
            if not video_id and extracted:
                await progress(
                    "uploading",
                    None,
                    "Mencari video_id baru di Player4Me\u2026",
                )
                try:
                    found = await self.player4me.find_recent_video_by_name(
                        file_path.name,
                        folder_id=folder_id,
                        existing_ids=existing_ids,
                    )
                except Player4MeError as exc:
                    log.warning("find_recent_video gagal: %s", exc)
                    found = None
                if found and found.get("id"):
                    video_id = str(found.get("id"))
                    upload_res.video_ids.insert(0, video_id)

            # 4) Upload extracted subtitles (best-effort \u2014 one failure
            # doesn't poison the others, but we collect a summary).
            uploaded_subs: list[str] = []
            failed_subs: list[str] = []
            if extracted and not video_id:
                log.warning(
                    "Tidak dapat menentukan video_id Player4Me \u2014 "
                    "subtitle tidak di-upload (video sudah masuk)."
                )
                failed_subs = [
                    f"{s.language}: video_id tidak ditemukan"
                    for s in extracted
                ]
            elif video_id and extracted:
                for idx, sub in enumerate(extracted, start=1):
                    if cancel_event.is_set():
                        break
                    await progress(
                        "uploading",
                        None,
                        (
                            f"Upload subtitle {idx}/{len(extracted)} "
                            f"({sub.language})\u2026"
                        ),
                    )
                    try:
                        await self.player4me.upload_subtitle(
                            video_id,
                            sub.path,
                            language=sub.language,
                            name=sub.name,
                        )
                        uploaded_subs.append(sub.language)
                    except Player4MeError as exc:
                        log.warning(
                            "Upload subtitle %s gagal: %s", sub.path, exc
                        )
                        failed_subs.append(f"{sub.language}: {exc}")

            sub_summary = self._format_sub_summary(
                uploaded_subs, failed_subs, extracted
            )
            await self.queue._update(  # type: ignore[attr-defined]
                job,
                player4me_task_id=upload_res.task_id,
                player4me_video_id=video_id or upload_res.primary_video_id,
                player4me_status=upload_res.status,
                player4me_engine=upload_res.engine,
                status=JobStatus.COMPLETED,
                finished_at=time.time(),
                progress=100.0,
                progress_text=(
                    f"Upload selesai. Player4Me ({upload_res.engine}): "
                    f"{upload_res.status}. {sub_summary}"
                ),
            )
        finally:
            # Clean up the per-job subtitle temp dir whether we succeeded or not.
            try:
                shutil.rmtree(sub_dir, ignore_errors=True)
            except Exception as exc:
                log.debug("Cleanup sub_dir %s gagal: %s", sub_dir, exc)

    # ---- player4me + Drive folder + sidecar subs (Flow B) ------------- #

    async def _run_upload_player4me_folder(
        self,
        job: Job,
        progress: Callable[[str, float | None, str], Awaitable[None]],
        cancel_event: asyncio.Event,
    ) -> None:
        """Flow B: Drive folder URL \u2192 download files individually \u2192 upload.

        We:
          1. Resolve folder_id from ``job.source_url``.
          2. List the folder via Drive API (OAuth/SA).
          3. Pick the largest video file as the "main" video and treat every
             text subtitle file (.srt / .ass / .vtt) as a sidecar.
          4. Download each file individually into ``downloads/<job_id>/``.
          5. Snapshot Player4Me, TUS-upload the video, recover videoId.
          6. Upload each subtitle.
          7. Clean up the per-job download folder.
        """
        try:
            info = classify_link(
                job.source_url,
                allow_google_drive=self.cfg.download.allow_google_drive,
                allow_direct=self.cfg.download.allow_direct_link,
                allow_ytdlp=self.cfg.download.allow_ytdlp,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"URL tidak valid untuk folder Drive: {exc}"
            ) from exc
        if info.kind != "google_drive_folder" or not info.folder_id:
            raise RuntimeError(
                "URL bukan folder Google Drive (drive.google.com/drive/folders/...)."
            )

        client = GDriveAPIClient(self.cfg)
        if not (client.has_oauth_token() or client.has_service_account()):
            raise RuntimeError(
                "Listing folder Drive butuh OAuth user-token atau "
                "Service Account. Set GOOGLE_DRIVE_OAUTH_TOKEN_PATH atau "
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON di .env."
            )

        await progress("starting", 0.0, "Listing folder Drive\u2026")
        try:
            entries = await client.list_folder(info.folder_id)
        except GDriveAPIError as exc:
            raise RuntimeError(f"List folder Drive gagal: {exc}") from exc
        if not entries:
            raise RuntimeError(
                "Folder Drive kosong (atau service account tidak punya akses)."
            )

        # Filter out subfolders / Google Docs natives \u2014 we only handle
        # binary files (videos + sidecar subtitles). The list is small enough
        # that an O(n) scan is fine.
        videos: list[dict] = []
        subs: list[dict] = []
        skipped: list[str] = []
        for e in entries:
            mime = str(e.get("mimeType") or "")
            name = str(e.get("name") or "").strip()
            if not name:
                continue
            if mime == "application/vnd.google-apps.folder":
                skipped.append(f"{name} (subfolder)")
                continue
            if mime.startswith("application/vnd.google-apps."):
                skipped.append(f"{name} (gdoc native)")
                continue
            ext = Path(name).suffix.lower()
            if (
                is_video_extension(name, self.cfg.video_extensions)
                or mime.startswith("video/")
            ):
                videos.append(e)
            elif ext in {".srt", ".ass", ".ssa", ".vtt"}:
                subs.append(e)
            else:
                skipped.append(f"{name} ({mime or 'unknown'})")

        if not videos:
            raise RuntimeError(
                "Folder tidak punya file video (.mp4/.mkv/...). Isi: "
                + ", ".join(e.get("name", "?") for e in entries[:5])
            )

        # Largest video by size = main video. ``size`` is a string in Drive's
        # JSON (it can exceed 2\u00b3\u00b2 bytes), so int() is the safe cast.
        def _size_int(d: dict) -> int:
            try:
                return int(d.get("size") or 0)
            except (TypeError, ValueError):
                return 0

        videos.sort(key=_size_int, reverse=True)
        main_video = videos[0]
        # Any extra videos we'll skip with a warning to keep the flow simple.
        for extra in videos[1:]:
            skipped.append(f"{extra.get('name')} (video tambahan, di-skip)")

        await progress(
            "downloading",
            0.0,
            (
                f"Folder berisi: 1 video utama, {len(subs)} subtitle, "
                f"{len(skipped)} di-skip"
            ),
        )

        # Per-job download dir keeps things tidy on cleanup. We use the
        # configured downloads_dir as the parent so cap-on-disk monitoring
        # still applies.
        job_dir = self.cfg.paths.download_dir / f"folder-{job.job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Download main video.
            video_name = safe_filename(str(main_video.get("name") or "video.mp4"))
            video_path = job_dir / video_name
            await progress(
                "downloading",
                0.0,
                f"Download video utama \u2014 {video_name}\u2026",
            )
            await client.download(
                str(main_video["id"]),
                video_path,
                progress_cb=progress,
                cancel_event=cancel_event,
            )
            video_size = video_path.stat().st_size if video_path.exists() else 0
            await self.queue._update(  # type: ignore[attr-defined]
                job,
                file_path=str(video_path),
                file_size_bytes=video_size,
                status=JobStatus.DOWNLOADED,
                progress=100.0,
                progress_text=(
                    f"Video utama download selesai ({human_bytes(video_size)})"
                ),
            )
            if not is_uploadable_video(video_path, self.cfg):
                raise RuntimeError(
                    f"File {video_path.name} bukan video yang valid "
                    f"(ekstensi tidak didukung)."
                )

            # Download every subtitle file individually.
            sub_paths: list[tuple[Path, dict]] = []
            default_lang = (
                self.cfg.upload_targets.player4me_default_subtitle_language
            )
            for idx, sub in enumerate(subs, start=1):
                if cancel_event.is_set():
                    break
                sub_name = safe_filename(str(sub.get("name") or f"sub-{idx}.srt"))
                sub_path = job_dir / sub_name
                await progress(
                    "downloading",
                    None,
                    f"Download subtitle {idx}/{len(subs)} \u2014 {sub_name}\u2026",
                )
                try:
                    await client.download(
                        str(sub["id"]),
                        sub_path,
                        progress_cb=None,
                        cancel_event=cancel_event,
                    )
                    sub_paths.append((sub_path, sub))
                except GDriveAPIError as exc:
                    log.warning(
                        "Download subtitle %s gagal: %s", sub_name, exc
                    )

            # Snapshot, upload video via TUS, recover videoId, attach subs.
            folder_id = (
                self.cfg.upload_targets.player4me_default_folder_id or None
            )
            await progress(
                "uploading",
                0.0,
                "Snapshot daftar video Player4Me sebelum upload\u2026",
            )
            try:
                existing_ids = await self.player4me.snapshot_video_ids(
                    folder_id=folder_id
                )
            except Player4MeError as exc:
                log.warning(
                    "snapshot video gagal (%s) \u2014 lanjut tanpa snapshot", exc
                )
                existing_ids = set()

            try:
                upload_res = await self.player4me.upload_local_file(
                    video_path,
                    title=job.title,
                    folder_id=folder_id,
                    progress_cb=progress,
                    cancel_event=cancel_event,
                )
            except Player4MeError as exc:
                raise RuntimeError(
                    f"Upload Player4Me (TUS) gagal: {exc}"
                ) from exc

            video_id = upload_res.primary_video_id
            if not video_id and sub_paths:
                await progress(
                    "uploading",
                    None,
                    "Mencari video_id baru di Player4Me\u2026",
                )
                try:
                    found = await self.player4me.find_recent_video_by_name(
                        video_path.name,
                        folder_id=folder_id,
                        existing_ids=existing_ids,
                    )
                except Player4MeError as exc:
                    log.warning("find_recent_video gagal: %s", exc)
                    found = None
                if found and found.get("id"):
                    video_id = str(found.get("id"))
                    upload_res.video_ids.insert(0, video_id)

            uploaded_subs: list[str] = []
            failed_subs: list[str] = []
            if sub_paths and not video_id:
                log.warning(
                    "Tidak dapat menentukan video_id Player4Me \u2014 "
                    "subtitle tidak di-upload (video sudah masuk)."
                )
                failed_subs = [
                    f"{p.name}: video_id tidak ditemukan"
                    for p, _ in sub_paths
                ]
            elif video_id and sub_paths:
                for idx, (sub_path, sub_meta) in enumerate(
                    sub_paths, start=1
                ):
                    if cancel_event.is_set():
                        break
                    lang, label = detect_language_from_filename(
                        sub_path.name, default=default_lang
                    )
                    await progress(
                        "uploading",
                        None,
                        (
                            f"Upload subtitle {idx}/{len(sub_paths)} "
                            f"({lang}) \u2014 {sub_path.name}\u2026"
                        ),
                    )
                    try:
                        await self.player4me.upload_subtitle(
                            video_id,
                            sub_path,
                            language=lang,
                            name=label,
                        )
                        uploaded_subs.append(lang)
                    except Player4MeError as exc:
                        log.warning(
                            "Upload subtitle %s gagal: %s", sub_path, exc
                        )
                        failed_subs.append(f"{sub_path.name}: {exc}")

            sub_summary = self._format_sub_summary(
                uploaded_subs, failed_subs, sub_paths
            )
            await self.queue._update(  # type: ignore[attr-defined]
                job,
                player4me_task_id=upload_res.task_id,
                player4me_video_id=video_id or upload_res.primary_video_id,
                player4me_status=upload_res.status,
                player4me_engine=upload_res.engine,
                status=JobStatus.COMPLETED,
                finished_at=time.time(),
                progress=100.0,
                progress_text=(
                    f"Upload selesai. Player4Me ({upload_res.engine}): "
                    f"{upload_res.status}. {sub_summary}"
                ),
            )
        finally:
            if self.cfg.app.auto_delete_after_upload:
                try:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    log.info("Auto-deleted folder %s", job_dir)
                except Exception as exc:
                    log.debug("Cleanup folder %s gagal: %s", job_dir, exc)

    @staticmethod
    def _format_sub_summary(
        uploaded: list[str],
        failed: list[str],
        attempted,
    ) -> str:
        """Build a one-line subtitle outcome summary for progress messages."""
        n_attempted = len(attempted) if hasattr(attempted, "__len__") else 0
        if not n_attempted:
            return "Tanpa subtitle."
        if uploaded and not failed:
            return f"Subtitle: {len(uploaded)}/{n_attempted} OK ({', '.join(uploaded)})."
        if uploaded and failed:
            return (
                f"Subtitle: {len(uploaded)}/{n_attempted} OK "
                f"({', '.join(uploaded)}); {len(failed)} gagal."
            )
        return f"Subtitle: 0/{n_attempted} OK \u2014 semua gagal."

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
        elif job.type == JobType.MIRROR_GDRIVE.value:
            keyword_hint = (
                f"\nFilter keyword: <code>{html.escape(job.tgindex_keyword)}</code>"
                if job.tgindex_keyword
                else ""
            )
            link_line = (
                f"\nLink: {job.gdrive_web_link}"
                if job.gdrive_web_link
                else ""
            )
            text = (
                "<b>Mirror ke Google Drive Berhasil</b>\n\n"
                f"Judul: {html.escape(job.title)}\n"
                f"File ID: <code>{html.escape(job.gdrive_file_id or '-')}</code>"
                f"{link_line}{keyword_hint}"
            )
        elif job.type in (
            JobType.UPLOAD_PLAYER4ME.value,
            JobType.UPLOAD_PLAYER4ME_SUBS.value,
            JobType.UPLOAD_PLAYER4ME_FOLDER.value,
        ):
            engine_label = {
                "advance_upload": "URL ingest (server-side)",
                "tus": "TUS upload (lokal)",
            }.get(job.player4me_engine or "", job.player4me_engine or "-")
            mode_label = {
                JobType.UPLOAD_PLAYER4ME.value: "video saja",
                JobType.UPLOAD_PLAYER4ME_SUBS.value: "video + sub embed (extracted)",
                JobType.UPLOAD_PLAYER4ME_FOLDER.value: "video + sub file (folder Drive)",
            }.get(job.type, job.type)
            text = (
                "<b>Upload Player4Me Berhasil</b>\n\n"
                f"Mode:\n{html.escape(mode_label)}\n\n"
                f"Judul:\n{html.escape(job.title)}\n\n"
                f"Engine:\n{html.escape(engine_label)}\n\n"
                f"Status:\n{html.escape(job.player4me_status or '-')}\n\n"
                f"Task ID:\n<code>{html.escape(job.player4me_task_id or '-')}</code>\n\n"
                f"Video ID:\n<code>{html.escape(job.player4me_video_id or '-')}</code>\n\n"
                f"Detail:\n{html.escape(job.progress_text or '-')}\n\n"
                "Catatan:\nVideo mungkin masih encoding beberapa menit "
                "di player4me."
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
