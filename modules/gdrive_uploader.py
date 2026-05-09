"""Google Drive uploader — upload file lokal ke folder Google Drive.

Menggunakan OAuth user token (token.pickle) yang sama dengan yang dipakai
GDriveAPIClient untuk download. Upload via Drive API v3 multipart upload
untuk file < 5 MB, dan resumable upload untuk file lebih besar.

Auth priority: OAuth user token > Service Account (tidak support API key untuk upload).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .config_manager import AppConfig
from .gdrive_api import GDriveAPIClient, GDriveAPIError, _load_oauth_credentials, _save_oauth_credentials
from .logger import get_logger
from .storage_manager import human_bytes

log = get_logger(__name__)

ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]

DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# Resumable upload chunk size: 8 MiB (harus kelipatan 256 KiB)
CHUNK_SIZE = 8 * 1024 * 1024


class GDriveUploadError(RuntimeError):
    """Raised on any Google Drive upload failure."""


class GDriveUploader:
    """Upload file lokal ke Google Drive folder menggunakan OAuth/SA."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        # Pakai client yang sama untuk auth (ambil access token dari sana)
        self._auth_client = GDriveAPIClient(cfg)

    def is_configured(self) -> bool:
        # Upload butuh OAuth atau SA — API key tidak bisa upload
        return (
            self._auth_client.has_oauth_token()
            or self._auth_client.has_service_account()
        )

    async def upload_file(
        self,
        file_path: str | Path,
        *,
        folder_id: Optional[str] = None,
        filename_override: Optional[str] = None,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> dict[str, Any]:
        """Upload *file_path* ke Google Drive.

        Parameters
        ----------
        folder_id:
            ID folder tujuan di Drive. Kalau None, file masuk ke root Drive.
        filename_override:
            Nama file di Drive. Kalau None, pakai nama file asli.

        Returns
        -------
        dict dengan keys: id, name, webViewLink, webContentLink
        """
        if not self.is_configured():
            raise GDriveUploadError(
                "Upload ke Drive butuh OAuth user token atau Service Account. "
                "Set GOOGLE_DRIVE_OAUTH_TOKEN_PATH di .env."
            )

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise GDriveUploadError(f"File tidak ditemukan: {path}")

        size = path.stat().st_size
        if size == 0:
            raise GDriveUploadError("File kosong, tidak bisa di-upload")

        name = filename_override or path.name
        mime = _guess_mime(path)

        await _emit(
            progress_cb, "uploading", 0.0,
            f"Memulai upload ke Google Drive: {name} ({human_bytes(size)})"
        )

        # Ambil access token
        headers = await self._auth_client._auth_headers()
        if not headers:
            raise GDriveUploadError("Tidak bisa mendapatkan access token Drive")

        # Gunakan resumable upload untuk semua ukuran (lebih reliable)
        upload_url = await self._initiate_resumable_upload(
            name=name,
            mime=mime,
            folder_id=folder_id,
            size=size,
            headers=headers,
        )

        # Upload chunks
        metadata = await self._resumable_upload_chunks(
            upload_url=upload_url,
            file_path=path,
            size=size,
            headers=headers,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )

        await _emit(
            progress_cb, "uploaded", 100.0,
            f"Upload ke Drive selesai: {name}"
        )
        log.info("Drive upload selesai: %s (id=%s)", name, metadata.get("id"))
        return metadata

    async def _initiate_resumable_upload(
        self,
        *,
        name: str,
        mime: str,
        folder_id: Optional[str],
        size: int,
        headers: dict[str, str],
    ) -> str:
        """Initiate resumable upload session, return upload URL."""
        url = f"{DRIVE_UPLOAD_BASE}/files?uploadType=resumable&supportsAllDrives=true"

        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": mime,
        }
        if folder_id:
            metadata["parents"] = [folder_id]

        req_headers = {
            **headers,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        }

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                url,
                json=metadata,
                headers=req_headers,
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    raise GDriveUploadError(
                        f"Gagal initiate resumable upload (HTTP {resp.status}): {body[:300]}"
                    )
                upload_url = resp.headers.get("Location") or resp.headers.get("location")
                if not upload_url:
                    raise GDriveUploadError("Tidak ada Location header di response initiate upload")
                return upload_url

    async def _resumable_upload_chunks(
        self,
        *,
        upload_url: str,
        file_path: Path,
        size: int,
        headers: dict[str, str],
        progress_cb: ProgressCB,
        cancel_event: Optional[asyncio.Event],
    ) -> dict[str, Any]:
        """Upload file in chunks ke resumable upload URL. Returns final metadata."""
        offset = 0
        last_pct = -1
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)

        async with aiohttp.ClientSession(timeout=timeout) as s:
            with file_path.open("rb") as fh:
                while offset < size:
                    if cancel_event is not None and cancel_event.is_set():
                        raise GDriveUploadError("Job dibatalkan oleh user")

                    fh.seek(offset)
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break

                    end = offset + len(chunk) - 1
                    chunk_headers = {
                        **headers,
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                        "Content-Type": "application/octet-stream",
                    }

                    async with s.put(
                        upload_url,
                        data=chunk,
                        headers=chunk_headers,
                    ) as resp:
                        # 308 = Resume Incomplete (chunk diterima, lanjut)
                        # 200/201 = Upload selesai
                        if resp.status == 308:
                            range_header = resp.headers.get("Range") or ""
                            if range_header and "-" in range_header:
                                offset = int(range_header.split("-")[1]) + 1
                            else:
                                offset += len(chunk)
                        elif resp.status in (200, 201):
                            body = await resp.text()
                            try:
                                return json.loads(body)
                            except json.JSONDecodeError:
                                return {"id": "", "name": file_path.name, "raw": body}
                        else:
                            body = await resp.text()
                            raise GDriveUploadError(
                                f"Upload chunk gagal (HTTP {resp.status}): {body[:300]}"
                            )

                    pct = (offset / size) * 100 if size else 0
                    ipct = int(pct)
                    if ipct != last_pct and ipct % 5 == 0:
                        last_pct = ipct
                        await _emit(
                            progress_cb, "uploading", pct,
                            f"Upload Drive {ipct}% ({human_bytes(offset)}/{human_bytes(size)})"
                        )

        raise GDriveUploadError("Upload selesai tapi tidak ada response metadata dari Drive")

    async def create_folder(
        self,
        name: str,
        *,
        parent_folder_id: Optional[str] = None,
    ) -> str:
        """Buat folder di Drive, return folder_id."""
        headers = await self._auth_client._auth_headers()
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_folder_id:
            metadata["parents"] = [parent_folder_id]

        url = f"{DRIVE_API_BASE}/files?supportsAllDrives=true"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                url,
                json=metadata,
                headers={**headers, "Content-Type": "application/json"},
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise GDriveUploadError(
                        f"Buat folder gagal (HTTP {resp.status}): {body[:300]}"
                    )
                data = json.loads(body)
                folder_id = data.get("id")
                if not folder_id:
                    raise GDriveUploadError(f"Buat folder tidak mengembalikan id: {data}")
                log.info("Drive folder dibuat: %s (id=%s)", name, folder_id)
                return folder_id

    async def find_or_create_folder(
        self,
        name: str,
        *,
        parent_folder_id: Optional[str] = None,
    ) -> str:
        """Cari folder dengan nama *name* di parent, buat kalau belum ada."""
        headers = await self._auth_client._auth_headers()
        # Escape single quotes in name for the query
        safe_name = name.replace("'", "\\'")
        parent_clause = (
            f" and '{parent_folder_id}' in parents"
            if parent_folder_id
            else ""
        )
        q = (
            f"name='{safe_name}' and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false{parent_clause}"
        )
        url = f"{DRIVE_API_BASE}/files"
        params = {
            "q": q,
            "fields": "files(id,name)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, params=params, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise GDriveUploadError(
                        f"Search folder gagal (HTTP {resp.status}): {body[:300]}"
                    )
                data = json.loads(body)
                files = data.get("files") or []
                if files:
                    folder_id = files[0]["id"]
                    log.info("Folder ditemukan: %s (id=%s)", name, folder_id)
                    return folder_id

        # Tidak ditemukan, buat baru
        return await self.create_folder(name, parent_folder_id=parent_folder_id)

    async def health_check(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "GDrive Upload tidak dikonfigurasi (butuh OAuth token atau SA)"
        try:
            await self._auth_client._get_access_token()
            return True, f"OK [{self._auth_client.auth_mode()}]"
        except Exception as exc:
            return False, str(exc)


# ----- helpers -------------------------------------------------------------- #

_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".srt": "application/x-subrip",
    ".ass": "text/x-ssa",
    ".vtt": "text/vtt",
}


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    import mimetypes
    guess, _ = mimetypes.guess_type(path.name)
    return guess or "application/octet-stream"


async def _emit(cb: ProgressCB, stage: str, percent: Optional[float], message: str) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        log.debug("progress_cb raised %s", exc)
