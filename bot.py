"""ZAEIN Automation Bot — entrypoint.

Usage::

    python bot.py
"""

from __future__ import annotations

import sys

from telegram.ext import Application, ApplicationBuilder

from modules.config_manager import (
    AppConfig,
    ConfigError,
    ensure_runtime_dirs,
    load_config,
    validate_secrets,
)
from modules.logger import get_logger, register_secrets, setup_logging
from modules.telegram_handlers import BotApp
from modules.live_queue_handlers import install_live_queue_handlers
from modules.advanced_mirror_handlers import install_advanced_mirror_handlers
from modules.player4me_auto_subs import install_player4me_auto_subs
from modules.reply_drive_handlers import install_reply_drive_handlers
from modules.bot_http_api import start_bot_http_api


def _build_application(cfg: AppConfig) -> Application:
    builder = ApplicationBuilder().token(cfg.secrets.telegram_bot_token)
    return builder.build()


async def _post_init(application: Application) -> None:
    bot_app: BotApp = application.bot_data["bot_app"]
    await bot_app.start()
    await start_bot_http_api(bot_app)


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[FATAL] Konfigurasi tidak valid: {exc}", file=sys.stderr)
        return 2

    ensure_runtime_dirs(cfg)

    setup_logging(
        log_dir=cfg.paths.log_dir,
        level=cfg.logging.level,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
        fmt=cfg.logging.format,
    )
    register_secrets(cfg.secrets.values_to_redact())

    log = get_logger("bot")
    log.info("=== %s starting ===", cfg.app.name)

    problems = validate_secrets(cfg)
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        if not cfg.secrets.telegram_bot_token or not cfg.secrets.admin_telegram_ids:
            log.error("Tidak bisa start bot tanpa TELEGRAM_BOT_TOKEN & ADMIN_TELEGRAM_ID.")
            return 2

    application = _build_application(cfg)

    bot_app = BotApp(cfg, application)

    # Add-on handlers are installed with negative groups so they can intercept
    # UX shortcuts before the older /m and unknown-command handlers.
    install_live_queue_handlers(bot_app)
    install_advanced_mirror_handlers(bot_app)
    install_player4me_auto_subs(bot_app)
    install_reply_drive_handlers(bot_app)

    application.post_init = _post_init  # type: ignore[assignment]

    log.info("Bot ready, mulai polling…")
    try:
        application.run_polling(
            allowed_updates=None,
            stop_signals=None if sys.platform.startswith("win") else None,
        )
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown diterima, keluar…")
    except Exception:
        log.exception("Bot crashed dengan exception fatal")
        return 1

    return 0


if __name__ == "__main__":
    # Do not force WindowsSelectorEventLoopPolicy here.
    # asyncio subprocess is needed for ffprobe/ffmpeg subtitle extraction on Windows.
    raise SystemExit(main())
