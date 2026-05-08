"""Google Drive API v3 client used as the preferred Drive download engine.

Two auth modes are supported, in this priority order:

1. **Service Account** (``GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`` in .env points to
   a JSON key file). Works for public files, and for any file/folder/Shared
   Drive that has been explicitly shared with the service account email.
2. **API Key** (``GOOGLE_DRIVE_API_KEY`` in .env). Public files only.

When neither is configured the client reports ``is_configured() == False`` and
the downloader falls back to ``gdown``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .config_manager import AppConfig
from .logger import get_logger
from .storage_manager import ensure_unique_path, human_bytes, safe_unlink
from .validators import safe_filename

log = get_logger(__name__)


ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class GDriveAPIError(RuntimeError):
    """Raised on any Google Drive API failure."""


class GDriveAPIClient:
    """Thin wrapper around Drive v3 ``files.get`` for metadata + media download."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._api_key = cfg.secrets.google_drive_api_key
        self._sa_json_path = cfg.secrets.google_drive_service_account_json
        # cached service-account access token
        self._access_token: str | None = None
        self._access_token_exp: float = 0.0

    # ----- configuration helpers ------------------------------------------- #

    def is_configured(self) -> bool:
        return self.has_service_account() or bool(self._api_key)

    def has_service_account(self) -> bool:
        if not self._sa_json_path:
            return False
        return Path(self._sa_json_path).expanduser().is_file()

    def auth_mode(self) -> str:
        if self.has_service_account():
            return "service_account"
        if self._api_key:
            return "api_key"
        return "disabled"

    # ----- access token (service account) ---------------------------------- #

    async def _get_access_token(self) -> str:
        if self._access_token and self._access_token_exp - 60 > time.time():
            return self._access_token

        if not self.has_service_account():
            raise GDriveAPIError("Service account JSON tidak dikonfigurasi")

        try:
            from google.auth.transport.requests import Request  # type: ignore
            from google.oauth2 import service_account  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised on missing dep
            raise GDriveAPIError(
                "Library `google-auth` tidak terinstall. "
                "Jalankan: pip install google-auth"
            ) from exc

        sa_path = str(Path(self._sa_json_path).expanduser())

        def _refresh() -> tuple[str, float]:
            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=[OAUTH_SCOPE]
            )
            creds.refresh(Request())
            exp = creds.expiry.timestamp() if creds.expiry else time.time() + 3600
            return creds.token, exp

        token, exp = await asyncio.to_thread(_refresh)
        self._access_token = token
        self._access_token_exp = exp
        return token

    async def _auth_headers(self) -> dict[str, str]:
        if self.has_service_account():
            token = await self._get_access_token()
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _params(self, base: dict[str, str]) -> dict[str, str]:
        if self.has_service_account():
            return base
        if self._api_key:
            return {**base, "key": self._api_key}
        return base

    # ----- public API ------------------------------------------------------ #

    async def get_metadata(self, file_id: str) -> dict[str, Any]:
        """Fetch ``files.get`` metadata for *file_id*."""
        url = f"{DRIVE_API_BASE}/files/{file_id}"
        params = self._params(
            {
                "fields": "id,name,size,mimeType,md5Checksum",
                "supportsAllDrives": "true",
            }
        )
        headers = await self._auth_headers()
        timeout = aiohttp.ClientTimeout(
            total=self.cfg.download.gdrive_api_timeout_seconds
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                body = await resp.text()
                if resp.status == 401 or resp.status == 403:
                    raise GDriveAPIError(
                        _format_api_error("metadata", resp.status, body)
                    )
                if resp.status == 404:
                    raise GDriveAPIError(
                        "File tidak ditemukan / tidak diizinkan untuk akun ini "
                        "(404). Untuk file private, share dulu ke email service "
                        "account."
                    )
                if resp.status >= 400:
                    raise GDriveAPIError(
                        _format_api_error("metadata", resp.status, body)
                    )
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise GDriveAPIError(
                        f"Metadata bukan JSON valid: {body[:200]}"
                    ) from exc

    async def health_check(self) -> tuple[bool, str]:
        """Light probe used by ``/health``."""
        if not self.is_configured():
            return False, "Tidak dikonfigurasi"
        if self.has_service_account():
            try:
                await self._get_access_token()
                return True, "Service Account OK"
            except GDriveAPIError as exc:
                return False, str(exc)
            except Exception as exc:  # pragma: no cover
                return False, f"Service Account error: {exc}"
        return True, "API Key tersedia (akan dites saat download)"

    async def download(
        self,
        file_id: str,
        title: str,
        *,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> tuple[Path, int, dict[str, Any]]:
        """Download Drive file *file_id* to ``cfg.paths.download_dir``.

        Returns ``(target_path, bytes_written, metadata)``. The target filename
        is built from *title* + the extension parsed from the Drive file name
        (or ``.bin`` if no extension is present).
        """
        meta = await self.get_metadata(file_id)
        original_name = str(meta.get("name") or "")
        ext = Path(safe_filename(original_name) or "file").suffix or ".bin"
        target = ensure_unique_path(
            self.cfg.paths.download_dir / f"{title}{ext}"
        )
        tmp = self.cfg.paths.temp_dir / (target.name + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)

        declared_total = int(meta.get("size") or 0) or None
        max_bytes = self.cfg.download.max_file_size_gb * (1024 ** 3)
        if declared_total and max_bytes and declared_total > max_bytes:
            raise GDriveAPIError(
                f"File ({human_bytes(declared_total)}) melebihi limit "
                f"{self.cfg.download.max_file_size_gb} GB"
            )

        url = f"{DRIVE_API_BASE}/files/{file_id}"
        params = self._params(
            {"alt": "media", "supportsAllDrives": "true"}
        )
        headers = await self._auth_headers()
        chunk_size = max(64 * 1024, self.cfg.download.gdrive_api_chunk_size_bytes)

        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=300
        )

        await _emit(
            progress_cb,
            "downloading",
            0.0,
            f"Mulai download via Google Drive API "
            f"(engine: GDriveAPI, mode: {self.auth_mode()})",
        )

        written = 0
        last_pct = -1
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, params=params, headers=headers
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise GDriveAPIError(
                            _format_api_error("download", resp.status, body)
                        )
                    total = int(resp.headers.get("Content-Length") or 0) or declared_total
                    if total and max_bytes and total > max_bytes:
                        raise GDriveAPIError(
                            f"File ({human_bytes(total)}) melebihi limit "
                            f"{self.cfg.download.max_file_size_gb} GB"
                        )
                    with tmp.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue
                            if cancel_event is not None and cancel_event.is_set():
                                raise GDriveAPIError("Job dibatalkan oleh user")
                            f.write(chunk)
                            written += len(chunk)
                            if max_bytes and written > max_bytes:
                                raise GDriveAPIError(
                                    f"File melewati limit "
                                    f"{self.cfg.download.max_file_size_gb} GB"
                                )
                            if total:
                                pct = (written / total) * 100
                                ipct = int(pct)
                                if ipct != last_pct and ipct % 5 == 0:
                                    last_pct = ipct
                                    await _emit(
                                        progress_cb,
                                        "downloading",
                                        pct,
                                        f"GDriveAPI {ipct}% "
                                        f"({human_bytes(written)}/{human_bytes(total)})",
                                    )
            tmp.replace(target)
        except Exception:
            safe_unlink(tmp)
            raise

        size = target.stat().st_size
        await _emit(
            progress_cb,
            "downloaded",
            100.0,
            f"GDriveAPI selesai: {target.name} ({human_bytes(size)})",
        )
        return target, size, meta


# ----- helpers -------------------------------------------------------------- #

def _format_api_error(stage: str, status: int, body: str) -> str:
    snippet = body.strip()[:300]
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or snippet
            reason = ""
            errors = err.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    reason = first.get("reason") or ""
            tag = f" ({reason})" if reason else ""
            return f"GDrive API {stage} HTTP {status}{tag}: {msg}"
    except json.JSONDecodeError:
        pass
    return f"GDrive API {stage} HTTP {status}: {snippet}"


async def _emit(
    cb: ProgressCB, stage: str, percent: Optional[float], message: str
) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # progress callbacks must never break the download
        log.debug("progress_cb raised %s", exc)
