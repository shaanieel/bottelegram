"""Configuration loader for ``.env`` (secrets) and ``config.yaml`` (settings).

Everything is exposed via the :class:`AppConfig` dataclass so the rest of the
codebase has type-safe, attribute-style access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# ----- Repo paths (always relative to this file's parent's parent) ---------- #

REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _abs(path: str | os.PathLike[str]) -> Path:
    """Return *path* resolved against the repo root if it is relative."""
    p = Path(path)
    return p if p.is_absolute() else (REPO_ROOT / p)


# ----- Dataclasses ---------------------------------------------------------- #

@dataclass
class AppSection:
    name: str = "ZAEIN Automation Bot"
    timezone: str = "UTC"
    max_parallel_downloads: int = 1
    max_parallel_uploads: int = 1
    auto_delete_after_upload: bool = True
    history_size: int = 50


@dataclass
class PathsSection:
    download_dir: Path = field(default_factory=lambda: _abs("downloads"))
    log_dir: Path = field(default_factory=lambda: _abs("logs"))
    temp_dir: Path = field(default_factory=lambda: _abs("temp"))
    data_dir: Path = field(default_factory=lambda: _abs("data"))
    jobs_file: Path = field(default_factory=lambda: _abs("data/jobs.json"))


@dataclass
class BunnySection:
    library_id: str = ""
    cdn_hostname: str = ""
    api_base: str = "https://video.bunnycdn.com"
    request_timeout_seconds: int = 60
    upload_timeout_seconds: int = 7200


@dataclass
class DownloadSection:
    default_tool: str = "auto"
    allow_direct_link: bool = True
    allow_google_drive: bool = True
    allow_ytdlp: bool = True
    max_file_size_gb: int = 20
    request_timeout_seconds: int = 60
    chunk_size_bytes: int = 1024 * 1024
    user_agent: str = "Mozilla/5.0 (compatible; ZAEINBot/1.0)"
    prefer_gdrive_api: bool = True
    gdrive_api_timeout_seconds: int = 30
    gdrive_api_chunk_size_bytes: int = 8 * 1024 * 1024


@dataclass
class UploadTargetsSection:
    bunny_stream_enabled: bool = True
    player4me_enabled: bool = True
    local_only_enabled: bool = True
    player4me_prefer_url_ingest: bool = True
    player4me_default_folder_id: str = ""


@dataclass
class LoggingSection:
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5
    format: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"


@dataclass
class Secrets:
    telegram_bot_token: str = ""
    admin_telegram_ids: tuple[int, ...] = ()
    bunny_api_key: str = ""
    google_drive_api_key: str = ""
    google_drive_service_account_json: str = ""
    google_drive_oauth_token_path: str = ""
    player4me_api_token: str = ""

    def values_to_redact(self) -> list[str]:
        out = [
            self.telegram_bot_token,
            self.bunny_api_key,
            self.google_drive_api_key,
            self.player4me_api_token,
        ]
        return [v for v in out if v]


@dataclass
class AppConfig:
    app: AppSection
    paths: PathsSection
    bunny: BunnySection
    download: DownloadSection
    upload_targets: UploadTargetsSection
    logging: LoggingSection
    video_extensions: tuple[str, ...]
    secrets: Secrets

    def safe_view(self) -> dict[str, Any]:
        """Serializable representation that does NOT include secrets."""
        return {
            "app": {
                "name": self.app.name,
                "timezone": self.app.timezone,
                "max_parallel_downloads": self.app.max_parallel_downloads,
                "max_parallel_uploads": self.app.max_parallel_uploads,
                "auto_delete_after_upload": self.app.auto_delete_after_upload,
                "history_size": self.app.history_size,
            },
            "paths": {
                "download_dir": str(self.paths.download_dir),
                "log_dir": str(self.paths.log_dir),
                "temp_dir": str(self.paths.temp_dir),
                "data_dir": str(self.paths.data_dir),
                "jobs_file": str(self.paths.jobs_file),
            },
            "bunny": {
                "library_id": self.bunny.library_id,
                "cdn_hostname": self.bunny.cdn_hostname,
                "api_base": self.bunny.api_base,
                "request_timeout_seconds": self.bunny.request_timeout_seconds,
                "upload_timeout_seconds": self.bunny.upload_timeout_seconds,
            },
            "download": {
                "default_tool": self.download.default_tool,
                "allow_direct_link": self.download.allow_direct_link,
                "allow_google_drive": self.download.allow_google_drive,
                "allow_ytdlp": self.download.allow_ytdlp,
                "max_file_size_gb": self.download.max_file_size_gb,
                "request_timeout_seconds": self.download.request_timeout_seconds,
                "chunk_size_bytes": self.download.chunk_size_bytes,
                "user_agent": self.download.user_agent,
                "prefer_gdrive_api": self.download.prefer_gdrive_api,
                "gdrive_api_chunk_size_bytes": self.download.gdrive_api_chunk_size_bytes,
            },
            "upload_targets": {
                "bunny_stream": {"enabled": self.upload_targets.bunny_stream_enabled},
                "player4me": {
                    "enabled": self.upload_targets.player4me_enabled,
                    "prefer_url_ingest": self.upload_targets.player4me_prefer_url_ingest,
                    "default_folder_id_set": bool(
                        self.upload_targets.player4me_default_folder_id
                    ),
                },
                "local_only": {"enabled": self.upload_targets.local_only_enabled},
            },
            "video_extensions": list(self.video_extensions),
            "logging": {
                "level": self.logging.level,
                "max_bytes": self.logging.max_bytes,
                "backup_count": self.logging.backup_count,
            },
            "admin_count": len(self.secrets.admin_telegram_ids),
            "telegram_bot_token_set": bool(self.secrets.telegram_bot_token),
            "bunny_api_key_set": bool(self.secrets.bunny_api_key),
            "gdrive_api_key_set": bool(self.secrets.google_drive_api_key),
            "gdrive_service_account_set": bool(
                self.secrets.google_drive_service_account_json
            ),
            "gdrive_oauth_token_set": bool(
                self.secrets.google_drive_oauth_token_path
            ),
            "player4me_api_token_set": bool(self.secrets.player4me_api_token),
        }


# ----- Loader --------------------------------------------------------------- #

class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def _parse_admin_ids(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            raise ConfigError(
                f"ADMIN_TELEGRAM_ID contains non-numeric value: {p!r}"
            )
    return tuple(out)


def load_config(
    config_path: str | os.PathLike[str] = "config.yaml",
    env_path: str | os.PathLike[str] | None = ".env",
) -> AppConfig:
    """Load .env + config.yaml and return a fully-populated :class:`AppConfig`."""
    cfg_file = _abs(config_path)
    if not cfg_file.exists():
        raise ConfigError(f"config.yaml not found at {cfg_file}")

    if env_path is not None:
        env_file = _abs(env_path)
        if env_file.exists():
            load_dotenv(env_file, override=False)

    with cfg_file.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    app_raw = raw.get("app", {}) or {}
    paths_raw = raw.get("paths", {}) or {}
    bunny_raw = raw.get("bunny", {}) or {}
    download_raw = raw.get("download", {}) or {}
    targets_raw = raw.get("upload_targets", {}) or {}
    logging_raw = raw.get("logging", {}) or {}

    app = AppSection(
        name=str(app_raw.get("name", "ZAEIN Automation Bot")),
        timezone=str(app_raw.get("timezone", "UTC")),
        max_parallel_downloads=int(app_raw.get("max_parallel_downloads", 1)),
        max_parallel_uploads=int(app_raw.get("max_parallel_uploads", 1)),
        auto_delete_after_upload=bool(app_raw.get("auto_delete_after_upload", True)),
        history_size=int(app_raw.get("history_size", 50)),
    )

    paths = PathsSection(
        download_dir=_abs(paths_raw.get("download_dir", "downloads")),
        log_dir=_abs(paths_raw.get("log_dir", "logs")),
        temp_dir=_abs(paths_raw.get("temp_dir", "temp")),
        data_dir=_abs(paths_raw.get("data_dir", "data")),
        jobs_file=_abs(paths_raw.get("jobs_file", "data/jobs.json")),
    )

    bunny = BunnySection(
        library_id=str(bunny_raw.get("library_id", "")).strip(),
        cdn_hostname=str(bunny_raw.get("cdn_hostname", "")).strip(),
        api_base=str(bunny_raw.get("api_base", "https://video.bunnycdn.com")).rstrip("/"),
        request_timeout_seconds=int(bunny_raw.get("request_timeout_seconds", 60)),
        upload_timeout_seconds=int(bunny_raw.get("upload_timeout_seconds", 7200)),
    )

    download = DownloadSection(
        default_tool=str(download_raw.get("default_tool", "auto")).lower(),
        allow_direct_link=bool(download_raw.get("allow_direct_link", True)),
        allow_google_drive=bool(download_raw.get("allow_google_drive", True)),
        allow_ytdlp=bool(download_raw.get("allow_ytdlp", True)),
        max_file_size_gb=int(download_raw.get("max_file_size_gb", 20)),
        request_timeout_seconds=int(download_raw.get("request_timeout_seconds", 60)),
        chunk_size_bytes=int(download_raw.get("chunk_size_bytes", 1024 * 1024)),
        user_agent=str(
            download_raw.get(
                "user_agent",
                "Mozilla/5.0 (compatible; ZAEINBot/1.0)",
            )
        ),
        prefer_gdrive_api=bool(download_raw.get("prefer_gdrive_api", True)),
        gdrive_api_timeout_seconds=int(
            download_raw.get("gdrive_api_timeout_seconds", 30)
        ),
        gdrive_api_chunk_size_bytes=int(
            download_raw.get("gdrive_api_chunk_size_bytes", 8 * 1024 * 1024)
        ),
    )

    p4m_raw = targets_raw.get("player4me") or {}
    targets = UploadTargetsSection(
        bunny_stream_enabled=bool(
            (targets_raw.get("bunny_stream") or {}).get("enabled", True)
        ),
        player4me_enabled=bool(p4m_raw.get("enabled", True)),
        local_only_enabled=bool(
            (targets_raw.get("local_only") or {}).get("enabled", True)
        ),
        player4me_prefer_url_ingest=bool(p4m_raw.get("prefer_url_ingest", True)),
        player4me_default_folder_id=str(
            p4m_raw.get("default_folder_id", "") or ""
        ).strip(),
    )

    log = LoggingSection(
        level=str(logging_raw.get("level", "INFO")).upper(),
        max_bytes=int(logging_raw.get("max_bytes", 5 * 1024 * 1024)),
        backup_count=int(logging_raw.get("backup_count", 5)),
        format=str(
            logging_raw.get(
                "format",
                "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            )
        ),
    )

    exts_raw = raw.get("video_extensions") or [
        ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
    ]
    video_extensions = tuple(str(e).lower() for e in exts_raw)

    secrets = Secrets(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        admin_telegram_ids=_parse_admin_ids(os.getenv("ADMIN_TELEGRAM_ID")),
        bunny_api_key=os.getenv("BUNNY_API_KEY", "").strip(),
        google_drive_api_key=os.getenv("GOOGLE_DRIVE_API_KEY", "").strip(),
        google_drive_service_account_json=os.getenv(
            "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", ""
        ).strip(),
        google_drive_oauth_token_path=os.getenv(
            "GOOGLE_DRIVE_OAUTH_TOKEN_PATH", ""
        ).strip(),
        player4me_api_token=os.getenv("PLAYER4ME_API_TOKEN", "").strip(),
    )

    return AppConfig(
        app=app,
        paths=paths,
        bunny=bunny,
        download=download,
        upload_targets=targets,
        logging=log,
        video_extensions=video_extensions,
        secrets=secrets,
    )


def ensure_runtime_dirs(cfg: AppConfig) -> None:
    """Create runtime directories (downloads, temp, logs, data) if missing."""
    for p in (
        cfg.paths.download_dir,
        cfg.paths.temp_dir,
        cfg.paths.log_dir,
        cfg.paths.data_dir,
    ):
        p.mkdir(parents=True, exist_ok=True)


def validate_secrets(cfg: AppConfig) -> list[str]:
    """Return a list of human-readable problems with secrets, empty if OK."""
    problems: list[str] = []
    if not cfg.secrets.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is empty in .env")
    if not cfg.secrets.admin_telegram_ids:
        problems.append("ADMIN_TELEGRAM_ID is empty in .env")
    if cfg.upload_targets.bunny_stream_enabled and not cfg.secrets.bunny_api_key:
        problems.append("BUNNY_API_KEY is empty but Bunny upload is enabled")
    if cfg.upload_targets.bunny_stream_enabled and not cfg.bunny.library_id:
        problems.append("bunny.library_id is empty in config.yaml")
    if cfg.upload_targets.player4me_enabled and not cfg.secrets.player4me_api_token:
        problems.append(
            "PLAYER4ME_API_TOKEN is empty but Player4Me upload is enabled"
        )
    return problems
