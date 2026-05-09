"""Player4Me upload client.

Two upload engines are exposed, both backed by the official Player4Me REST API:

1. **URL ingest** (preferred when the source URL is reachable by the
   Player4Me servers, e.g. a public Google Drive direct-download URL or a
   torrent magnet). Wraps ``POST /api/v1/video/advance-upload`` — the server
   pulls the file itself, so user upstream bandwidth is irrelevant. This is
   the *fastest* path; it routinely beats local TUS by orders of magnitude on
   slow connections.
2. **Local TUS upload** (fallback for files we already have on disk and for
   private sources that Player4Me cannot fetch). Uses the official Player4Me
   TUS endpoint (chunk size 52,428,800 bytes per their docs) over keep-alive
   aiohttp. PyCurl/curl give no measurable speed advantage over aiohttp here —
   the bottleneck is the upstream link, not the HTTP library.

API token authentication uses the ``api-token`` header. The token never
appears in any log message: it is registered with the redaction filter via
``Secrets.values_to_redact``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .config_manager import AppConfig
from .logger import get_logger
from .storage_manager import human_bytes

log = get_logger(__name__)

ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]


PLAYER4ME_API_BASE = "https://player4me.com/api/v1"
TUS_RESUMABLE = "1.0.0"


class Player4MeError(RuntimeError):
    """Raised on any Player4Me API failure."""


@dataclass
class Player4MeUploadResult:
    """Outcome of a Player4Me upload (URL-ingest or local TUS)."""

    task_id: Optional[str]
    video_ids: list[str]
    name: str
    status: str
    raw: dict[str, Any]
    engine: str  # "advance_upload" or "tus"

    @property
    def primary_video_id(self) -> Optional[str]:
        return self.video_ids[0] if self.video_ids else None


# --------------------------------------------------------------------------- #


class Player4MeUploader:
    """Async client for Player4Me's video upload + advance-upload endpoints."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._api_base = PLAYER4ME_API_BASE
        self._api_token = cfg.secrets.player4me_api_token

    # ---- public --------------------------------------------------------- #

    def is_configured(self) -> bool:
        return bool(self._api_token)

    async def health_check(self) -> tuple[bool, str]:
        """Light probe used by ``/health``: hits ``/billing/balance``."""
        if not self.is_configured():
            return False, "Player4Me tidak dikonfigurasi (PLAYER4ME_API_TOKEN kosong)"
        url = f"{self._api_base}/billing/balance"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers=self._headers_json(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        return False, "API token Player4Me ditolak (HTTP 401)"
                    if resp.status >= 400:
                        return False, f"HTTP {resp.status}"
                    return True, "OK"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ---- engine 1: server-side URL ingest ------------------------------- #

    async def submit_url_task(
        self,
        url: str,
        name: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> str:
        """Create an advance-upload task and return its ``task_id``."""
        self._require_config()
        endpoint = f"{self._api_base}/video/advance-upload"
        payload: dict[str, Any] = {"url": url}
        if name:
            payload["name"] = name
        if folder_id:
            payload["folderId"] = folder_id

        async with aiohttp.ClientSession() as s:
            async with s.post(
                endpoint,
                json=payload,
                headers=self._headers_json(),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400 or not isinstance(body, dict):
                    raise Player4MeError(
                        self._format_error("advance-upload create", resp.status, body)
                    )
                task_id = body.get("id")
                if not task_id:
                    raise Player4MeError(
                        f"advance-upload tidak mengembalikan task id: {body!r}"
                    )
                log.info(
                    "Player4Me advance-upload created: task=%s name=%s",
                    task_id,
                    name,
                )
                return str(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch the current status of an advance-upload task."""
        self._require_config()
        url = f"{self._api_base}/video/advance-upload/{task_id}"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers=self._headers_json(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400 or not isinstance(body, dict):
                    raise Player4MeError(
                        self._format_error("advance-upload status", resp.status, body)
                    )
                return body

    async def wait_for_task(
        self,
        task_id: str,
        *,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
        poll_interval: float = 5.0,
        timeout: float = 24 * 3600.0,
    ) -> dict[str, Any]:
        """Poll ``get_task`` until status is terminal, then return the body."""
        deadline = asyncio.get_event_loop().time() + timeout
        last_status = ""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise Player4MeError("Job dibatalkan oleh user")
            if asyncio.get_event_loop().time() > deadline:
                raise Player4MeError("Timeout menunggu Player4Me menyelesaikan ingest")
            body = await self.get_task(task_id)
            status = str(body.get("status") or "").strip()
            if status != last_status:
                last_status = status
                await _emit(
                    progress_cb,
                    "uploading",
                    None,
                    f"Player4Me ingest: {status or 'queued'}",
                )
            lower = status.lower()
            if lower in ("completed", "success", "done", "finished"):
                return body
            if lower in ("failed", "error", "rejected", "cancelled", "canceled"):
                err = body.get("error") or status
                raise Player4MeError(f"Player4Me ingest gagal: {err}")
            await asyncio.sleep(poll_interval)

    async def upload_via_url(
        self,
        source_url: str,
        name: str,
        *,
        folder_id: Optional[str] = None,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
        wait_until_done: bool = True,
    ) -> Player4MeUploadResult:
        """Convenience: submit advance-upload, optionally wait for completion."""
        await _emit(
            progress_cb,
            "uploading",
            None,
            "Player4Me URL ingest: kirim URL ke server Player4Me…",
        )
        task_id = await self.submit_url_task(source_url, name=name, folder_id=folder_id)

        if not wait_until_done:
            return Player4MeUploadResult(
                task_id=task_id,
                video_ids=[],
                name=name,
                status="queued",
                raw={"id": task_id},
                engine="advance_upload",
            )

        body = await self.wait_for_task(
            task_id, progress_cb=progress_cb, cancel_event=cancel_event
        )
        videos_raw = body.get("videos") or []
        video_ids = [str(v) for v in videos_raw if v]
        return Player4MeUploadResult(
            task_id=task_id,
            video_ids=video_ids,
            name=str(body.get("name") or name),
            status=str(body.get("status") or "completed"),
            raw=body,
            engine="advance_upload",
        )

    # ---- engine 2: local TUS upload ------------------------------------- #

    async def upload_local_file(
        self,
        file_path: str | Path,
        title: str,
        *,
        folder_id: Optional[str] = None,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Player4MeUploadResult:
        """Upload a local file via the Player4Me TUS endpoint.

        Implements a minimal TUS 1.0.0 client (Creation + Patch). 50 MiB chunks
        per Player4Me's docs; smaller chunks are accepted but slower.
        """
        self._require_config()
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise Player4MeError(f"File tidak ditemukan: {path}")
        size = path.stat().st_size
        if size == 0:
            raise Player4MeError("File kosong, tidak bisa di-upload")

        endpoints = await self._get_tus_endpoints()
        tus_url: str = endpoints["tusUrl"]
        access_token: str = endpoints["accessToken"]

        filetype = _guess_mime(path)
        metadata = {
            "accessToken": access_token,
            "filename": path.name,
            "filetype": filetype,
        }
        if folder_id:
            metadata["folderId"] = folder_id

        upload_url = await self._tus_create(tus_url, size, metadata)
        log.info(
            "Player4Me TUS create OK: %s (%s bytes -> %s)",
            path.name,
            size,
            upload_url,
        )

        await _emit(
            progress_cb,
            "uploading",
            0.0,
            f"Player4Me TUS upload mulai ({human_bytes(size)})",
        )

        chunk_size = 52_428_800  # 50 MiB per Player4Me docs
        offset = await self._tus_patch_all(
            upload_url=upload_url,
            file_path=path,
            total=size,
            chunk_size=chunk_size,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        if offset != size:
            raise Player4MeError(
                f"TUS upload selesai tapi offset {offset} != total {size}"
            )

        await _emit(progress_cb, "uploaded", 100.0, "Player4Me TUS upload selesai")
        return Player4MeUploadResult(
            task_id=None,
            video_ids=[],
            name=title or path.name,
            status="uploaded",
            raw={"upload_url": upload_url, "size": size, "filetype": filetype},
            engine="tus",
        )

    # ---- internal: TUS helpers ------------------------------------------ #

    async def _get_tus_endpoints(self) -> dict[str, str]:
        url = f"{self._api_base}/video/upload"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers=self._headers_json(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400 or not isinstance(body, dict):
                    raise Player4MeError(
                        self._format_error("get tus endpoints", resp.status, body)
                    )
                if not body.get("tusUrl") or not body.get("accessToken"):
                    raise Player4MeError(
                        f"Player4Me /video/upload tidak mengembalikan tusUrl: {body!r}"
                    )
                # Use the tusUrl exactly as the API returned it (incl. trailing
                # slash). The TUS server's reverse proxy mounts POST on the URL
                # with the trailing slash; stripping it caused HTTP 405 on the
                # creation request.
                return {
                    "tusUrl": str(body["tusUrl"]),
                    "accessToken": str(body["accessToken"]),
                }

    async def _tus_create(
        self, tus_url: str, total: int, metadata: dict[str, str]
    ) -> str:
        meta_header = ",".join(
            f"{k} {base64.b64encode(v.encode('utf-8')).decode('ascii')}"
            for k, v in metadata.items()
        )
        headers = {
            "Tus-Resumable": TUS_RESUMABLE,
            "Upload-Length": str(total),
            "Upload-Metadata": meta_header,
            "Content-Length": "0",
        }
        log.debug("Player4Me TUS create POST -> %s (size=%s)", tus_url, total)
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(
                    tus_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        redirect = resp.headers.get("Location") or resp.headers.get(
                            "location"
                        )
                        raise Player4MeError(
                            f"Player4Me TUS create HTTP {resp.status} redirect ke "
                            f"{redirect!r} dari {tus_url!r}. "
                            "Cek tusUrl di response /api/v1/video/upload — "
                            "trailing slash mungkin salah."
                        )
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        raise Player4MeError(
                            self._format_error(
                                f"TUS create [{tus_url}]", resp.status, body
                            )
                        )
                    location = resp.headers.get("Location") or resp.headers.get(
                        "location"
                    )
                    if not location:
                        raise Player4MeError(
                            "TUS create response tidak punya Location header"
                        )
                    # Resolve relative Location against tus base
                    if location.startswith("/"):
                        # https://host[:port]/<path> -> base host
                        from urllib.parse import urlparse, urlunparse

                        parsed = urlparse(tus_url)
                        location = urlunparse(
                            (parsed.scheme, parsed.netloc, location, "", "", "")
                        )
                    elif not location.startswith(("http://", "https://")):
                        location = f"{tus_url.rstrip('/')}/{location}"
                    return location
            except aiohttp.ClientError as exc:
                raise Player4MeError(
                    f"Player4Me TUS create network error ({tus_url}): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    async def _tus_patch_all(
        self,
        *,
        upload_url: str,
        file_path: Path,
        total: int,
        chunk_size: int,
        progress_cb: ProgressCB,
        cancel_event: Optional[asyncio.Event],
    ) -> int:
        offset = 0
        last_pct = -1
        # One persistent session for the whole upload — keep-alive matters here.
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=600
        )
        async with aiohttp.ClientSession(timeout=timeout) as s:
            with file_path.open("rb") as fh:
                while offset < total:
                    if cancel_event is not None and cancel_event.is_set():
                        raise Player4MeError("Job dibatalkan oleh user")
                    fh.seek(offset)
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    headers = {
                        "Tus-Resumable": TUS_RESUMABLE,
                        "Upload-Offset": str(offset),
                        "Content-Type": "application/offset+octet-stream",
                        "Content-Length": str(len(chunk)),
                    }
                    async with s.patch(
                        upload_url,
                        data=chunk,
                        headers=headers,
                    ) as resp:
                        if resp.status not in (200, 204):
                            body = await resp.text()
                            raise Player4MeError(
                                self._format_error(
                                    f"TUS patch (offset={offset})",
                                    resp.status,
                                    body,
                                )
                            )
                        new_offset_raw = resp.headers.get(
                            "Upload-Offset"
                        ) or resp.headers.get("upload-offset")
                        if new_offset_raw is None:
                            raise Player4MeError(
                                "TUS patch tidak mengembalikan Upload-Offset"
                            )
                        try:
                            new_offset = int(new_offset_raw)
                        except ValueError as exc:
                            raise Player4MeError(
                                f"Upload-Offset tidak valid: {new_offset_raw}"
                            ) from exc
                        if new_offset <= offset:
                            raise Player4MeError(
                                f"TUS server tidak maju (offset {offset} -> {new_offset})"
                            )
                        offset = new_offset

                    pct = (offset / total) * 100 if total else 0
                    ipct = int(pct)
                    if ipct != last_pct and (ipct % 5 == 0 or ipct == 100):
                        last_pct = ipct
                        await _emit(
                            progress_cb,
                            "uploading",
                            pct,
                            f"Player4Me TUS {ipct}% "
                            f"({human_bytes(offset)}/{human_bytes(total)})",
                        )
        return offset

    # ---- internal: misc ------------------------------------------------- #

    def _require_config(self) -> None:
        if not self._api_token:
            raise Player4MeError("PLAYER4ME_API_TOKEN kosong di .env")

    def _headers_json(self) -> dict[str, str]:
        return {
            "api-token": self._api_token,
            "accept": "application/json",
            "content-type": "application/json",
        }

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse):
        text = await resp.text()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _format_error(stage: str, status: int, body: object) -> str:
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error") or json.dumps(body)[:300]
            return f"Player4Me {stage} HTTP {status}: {msg}"
        if isinstance(body, str):
            return f"Player4Me {stage} HTTP {status}: {body[:300]}"
        return f"Player4Me {stage} HTTP {status}: {body!r}"


# --------------------------------------------------------------------------- #


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
}


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    # Fall back to stdlib mimetypes for non-video file types
    import mimetypes

    guess, _ = mimetypes.guess_type(path.name)
    return guess or "application/octet-stream"


async def _emit(
    cb: ProgressCB, stage: str, percent: Optional[float], message: str
) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # progress callbacks must never break the upload
        log.debug("progress_cb raised %s", exc)
