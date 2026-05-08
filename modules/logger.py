"""Logging utilities with rotating file handler.

Logs are written to ``logs/bot.log`` with rotation. We never log secrets such as
the Telegram bot token or the Bunny API key — anything that needs to be redacted
should pass through :func:`redact`.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any, Iterable

_INITIALIZED = False
_SECRET_PLACEHOLDERS: list[str] = []


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def setup_logging(
    log_dir: str | os.PathLike[str] = "logs",
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
) -> logging.Logger:
    """Configure root logger. Idempotent — safe to call multiple times."""
    global _INITIALIZED

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "bot.log"

    root = logging.getLogger()
    if _INITIALIZED:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return root

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=_coerce_int(max_bytes, 5 * 1024 * 1024),
        backupCount=_coerce_int(backup_count, 5),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RedactionFilter())

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_RedactionFilter())

    # Drop any pre-existing handlers (e.g., pytest, third-party libs).
    for h in list(root.handlers):
        root.removeHandler(h)

    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Quiet down noisy third-party libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    _INITIALIZED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def register_secrets(values: Iterable[str | None]) -> None:
    """Register secret strings that should be redacted from logs."""
    for v in values:
        if v and isinstance(v, str) and len(v) >= 4 and v not in _SECRET_PLACEHOLDERS:
            _SECRET_PLACEHOLDERS.append(v)


def redact(text: str) -> str:
    """Replace any registered secret with ``***`` in *text*."""
    if not text:
        return text
    out = text
    for s in _SECRET_PLACEHOLDERS:
        if s and s in out:
            out = out.replace(s, "***")
    return out


class _RedactionFilter(logging.Filter):
    """Logging filter that redacts registered secrets from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if not _SECRET_PLACEHOLDERS:
            return True
        redacted = redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = None
        return True
