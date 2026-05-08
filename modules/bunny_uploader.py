"""Bunny Stream API client.

Implements the three operations we need:

1. Create a video object       (POST /library/{id}/videos)
2. Upload binary               (PUT  /library/{id}/videos/{videoId})
3. Get video status            (GET  /library/{id}/videos/{videoId})

API key is read from :class:`AppConfig` (which loads it from .env). It is never
logged or echoed back to the user.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp

from .config_manager import AppConfig
from .logger import get_logger
from .storage_manager import human_bytes

log = get_logger(__name__)

ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]


# Bunny Stream "status" enum (see https://docs.bunny.net/reference/video_getvideo)
_STATUS_MAP = {
    0: "Created",
    1: "Uploaded",
    2: "Processing",
    3: "Transcoding",
    4: "Finished",
    5: "Error",
    6: "UploadFailed",
    7: "JitSegmenting",
    8: "JitPlaylistsCreated",
}


class BunnyError(RuntimeError):
    """Raised on any Bunny Stream API failure."""


@dataclass
class BunnyVideo:
    video_id: str
    title: str
    status: int
    status_text: str
    library_id: str
    cdn_hostname: str
    raw: dict

    @property
    def embed_url(self) -> str:
        return f"https://iframe.mediadelivery.net/embed/{self.library_id}/{self.video_id}"

    @property
    def play_url(self) -> str:
        return f"https://iframe.mediadelivery.net/play/{self.library_id}/{self.video_id}"


# --------------------------------------------------------------------------- #

class BunnyUploader:
    """Thin async wrapper around the Bunny Stream HTTP API."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._api_base = cfg.bunny.api_base.rstrip("/")
        self._library_id = cfg.bunny.library_id
        self._api_key = cfg.secrets.bunny_api_key
        self._cdn = cfg.bunny.cdn_hostname

    # ---- public ---------------------------------------------------------- #

    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._library_id)

    async def create_video(self, title: str) -> str:
        """Create a video object and return its videoId."""
        self._require_config()
        url = f"{self._api_base}/library/{self._library_id}/videos"
        payload = {"title": title}
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url,
                json=payload,
                headers=self._headers_json(),
                timeout=aiohttp.ClientTimeout(total=self.cfg.bunny.request_timeout_seconds),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400 or not isinstance(body, dict):
                    raise BunnyError(
                        f"create_video failed (HTTP {resp.status}): {body!r}"
                    )
                vid = body.get("guid") or body.get("videoLibraryId") or body.get("id")
                if not vid:
                    raise BunnyError(f"create_video tidak mengembalikan videoId: {body!r}")
                log.info("Bunny video created: %s (title=%s)", vid, title)
                return str(vid)

    async def upload_binary(
        self,
        video_id: str,
        file_path: str | Path,
        *,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> None:
        """PUT raw video binary to Bunny Stream."""
        self._require_config()
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise BunnyError(f"File tidak ditemukan: {path}")

        size = path.stat().st_size
        if size == 0:
            raise BunnyError("File kosong, tidak bisa di-upload")

        url = f"{self._api_base}/library/{self._library_id}/videos/{video_id}"
        chunk_size = max(256 * 1024, self.cfg.download.chunk_size_bytes)

        await _emit(progress_cb, "uploading", 0.0, f"Mulai upload Bunny ({human_bytes(size)})")

        sent = {"bytes": 0, "last_pct": -1}

        async def _stream():
            with path.open("rb") as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise BunnyError("Job dibatalkan oleh user")
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    sent["bytes"] += len(chunk)
                    pct = (sent["bytes"] / size) * 100 if size else 0
                    ipct = int(pct)
                    if ipct != sent["last_pct"] and ipct % 5 == 0:
                        sent["last_pct"] = ipct
                        await _emit(
                            progress_cb,
                            "uploading",
                            pct,
                            f"Upload {ipct}% ({human_bytes(sent['bytes'])}/{human_bytes(size)})",
                        )
                    yield chunk

        timeout = aiohttp.ClientTimeout(total=self.cfg.bunny.upload_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.put(
                url,
                data=_stream(),
                headers=self._headers_binary(size),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400:
                    raise BunnyError(
                        f"upload_binary failed (HTTP {resp.status}): {body!r}"
                    )
                log.info("Bunny upload finished: video_id=%s", video_id)
                await _emit(progress_cb, "uploaded", 100.0, "Upload Bunny selesai")

    async def get_video(self, video_id: str) -> BunnyVideo:
        """Fetch the current status of a video."""
        self._require_config()
        url = f"{self._api_base}/library/{self._library_id}/videos/{video_id}"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url,
                headers=self._headers_json(),
                timeout=aiohttp.ClientTimeout(total=self.cfg.bunny.request_timeout_seconds),
            ) as resp:
                body = await self._read_json(resp)
                if resp.status >= 400 or not isinstance(body, dict):
                    raise BunnyError(
                        f"get_video failed (HTTP {resp.status}): {body!r}"
                    )
                status_int = int(body.get("status", 0))
                return BunnyVideo(
                    video_id=str(body.get("guid", video_id)),
                    title=str(body.get("title", "")),
                    status=status_int,
                    status_text=_STATUS_MAP.get(status_int, f"Unknown({status_int})"),
                    library_id=self._library_id,
                    cdn_hostname=self._cdn,
                    raw=body,
                )

    async def upload_full(
        self,
        title: str,
        file_path: str | Path,
        *,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> BunnyVideo:
        """Convenience: create + upload + fetch initial status."""
        await _emit(progress_cb, "creating", None, "Membuat video object di Bunny Stream…")
        video_id = await self.create_video(title)
        await self.upload_binary(
            video_id,
            file_path,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
        video = await self.get_video(video_id)
        await _emit(
            progress_cb,
            "encoding",
            None,
            f"Status awal Bunny: {video.status_text}",
        )
        return video

    async def health_check(self) -> tuple[bool, str]:
        """Light ping to confirm API key + library ID are usable."""
        if not self.is_configured():
            return False, "Bunny tidak dikonfigurasi (API key / library ID kosong)"
        url = (
            f"{self._api_base}/library/{self._library_id}/videos"
            "?page=1&itemsPerPage=1"
        )
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers=self._headers_json(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        return False, "API key Bunny ditolak (HTTP 401)"
                    if resp.status >= 400:
                        return False, f"HTTP {resp.status}"
                    return True, "OK"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    # ---- internal -------------------------------------------------------- #

    def _require_config(self) -> None:
        if not self._library_id:
            raise BunnyError("bunny.library_id kosong di config.yaml")
        if not self._api_key:
            raise BunnyError("BUNNY_API_KEY kosong di .env")

    def _headers_json(self) -> dict[str, str]:
        return {
            "AccessKey": self._api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }

    def _headers_binary(self, content_length: int) -> dict[str, str]:
        return {
            "AccessKey": self._api_key,
            "accept": "application/json",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(content_length),
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


async def _emit(cb: ProgressCB, stage: str, percent: Optional[float], message: str) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        log.debug("progress_cb raised %s", exc)
