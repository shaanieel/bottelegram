"""Download manager — Google Drive, direct links, Dropbox, OneDrive, yt-dlp.

Each public function is async and accepts an optional ``progress_cb`` coroutine
``async def cb(stage: str, percent: float | None, message: str) -> None`` so
the queue manager can forward updates to Telegram.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from .config_manager import AppConfig
from .logger import get_logger
from .storage_manager import ensure_unique_path, human_bytes, safe_unlink
from .validators import (
    LinkInfo,
    classify_link,
    extract_google_drive_id,
    is_video_extension,
    safe_filename,
)

log = get_logger(__name__)

ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]


class DownloadError(RuntimeError):
    """Raised on any download failure."""


@dataclass
class DownloadResult:
    path: Path
    size_bytes: int
    source_kind: str
    tool_used: str


# ----- Public entrypoint ---------------------------------------------------- #

async def download(
    url: str,
    title: str,
    cfg: AppConfig,
    *,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> DownloadResult:
    """Detect the URL type and dispatch to the appropriate downloader."""
    info = classify_link(
        url,
        allow_google_drive=cfg.download.allow_google_drive,
        allow_direct=cfg.download.allow_direct_link,
        allow_ytdlp=cfg.download.allow_ytdlp,
    )

    safe_title = safe_filename(title or "download")
    log.info("Classified URL as %s -> %s", info.kind, info.url)
    await _emit(progress_cb, "starting", None, f"Mendeteksi jenis link: {info.kind}")

    if info.kind == "google_drive":
        try:
            return await _download_gdrive(info, safe_title, cfg, progress_cb, cancel_event)
        except DownloadError as exc:
            if cfg.download.allow_ytdlp:
                log.warning("Google Drive download failed (%s), falling back to yt-dlp", exc)
                await _emit(
                    progress_cb,
                    "fallback",
                    None,
                    f"gdown gagal: {exc}. Mencoba yt-dlp…",
                )
                return await _download_ytdlp(info, safe_title, cfg, progress_cb, cancel_event)
            raise

    if info.kind in ("dropbox", "onedrive", "direct"):
        return await _download_direct(info, safe_title, cfg, progress_cb, cancel_event)

    if info.kind == "ytdlp":
        if not cfg.download.allow_ytdlp:
            raise DownloadError("yt-dlp dinonaktifkan di config.yaml")
        return await _download_ytdlp(info, safe_title, cfg, progress_cb, cancel_event)

    raise DownloadError(f"Tipe link tidak didukung: {info.kind}")


# ----- Internal: emit progress safely -------------------------------------- #

async def _emit(cb: ProgressCB, stage: str, percent: Optional[float], message: str) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # progress callbacks must never break the download
        log.debug("progress_cb raised %s", exc)


def _check_cancel(cancel_event: Optional[asyncio.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadError("Job dibatalkan oleh user")


# ----- Google Drive (gdown) ------------------------------------------------- #

async def _download_gdrive(
    info: LinkInfo,
    title: str,
    cfg: AppConfig,
    progress_cb: ProgressCB,
    cancel_event: Optional[asyncio.Event],
) -> DownloadResult:
    file_id = info.file_id or extract_google_drive_id(info.url)
    if not file_id:
        raise DownloadError("Tidak bisa extract Google Drive FILE_ID")

    download_dir = cfg.paths.download_dir
    download_dir.mkdir(parents=True, exist_ok=True)

    await _emit(progress_cb, "downloading", None, f"Download Google Drive (id={file_id})")

    def _do() -> str:
        try:
            import gdown
        except ImportError as exc:
            raise DownloadError(f"gdown tidak terinstall: {exc}") from exc

        # gdown can resolve filename automatically when output ends with a directory.
        out_dir = str(download_dir) + os.sep
        try:
            saved = gdown.download(id=file_id, output=out_dir, quiet=True, fuzzy=True)
        except Exception as exc:  # gdown raises various exceptions
            raise DownloadError(f"gdown error: {exc}") from exc
        if not saved:
            raise DownloadError("gdown tidak menghasilkan file (mungkin dibatasi quota / private)")
        return saved

    saved_path_str = await asyncio.to_thread(_do)
    _check_cancel(cancel_event)

    saved_path = Path(saved_path_str)
    if not saved_path.exists():
        raise DownloadError("File hasil download tidak ditemukan")

    # Rename to user-provided title while keeping original extension
    desired_ext = saved_path.suffix or ".mp4"
    desired = ensure_unique_path(download_dir / f"{title}{desired_ext}")
    if saved_path.resolve() != desired.resolve():
        try:
            saved_path.rename(desired)
            saved_path = desired
        except OSError as exc:
            log.warning("Rename gagal (%s), pakai nama asli", exc)

    size = saved_path.stat().st_size
    _check_max_size(size, cfg, saved_path)

    await _emit(
        progress_cb,
        "downloaded",
        100.0,
        f"Download selesai: {saved_path.name} ({human_bytes(size)})",
    )
    return DownloadResult(
        path=saved_path,
        size_bytes=size,
        source_kind="google_drive",
        tool_used="gdown",
    )


# ----- Direct / Dropbox / OneDrive ----------------------------------------- #

_DISPOSITION_FILENAME_RE = re.compile(
    r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", re.I
)


async def _download_direct(
    info: LinkInfo,
    title: str,
    cfg: AppConfig,
    progress_cb: ProgressCB,
    cancel_event: Optional[asyncio.Event],
) -> DownloadResult:
    download_dir = cfg.paths.download_dir
    temp_dir = cfg.paths.temp_dir
    download_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": cfg.download.user_agent}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

    chunk_size = max(64 * 1024, cfg.download.chunk_size_bytes)
    max_bytes = cfg.download.max_file_size_gb * (1024 ** 3)

    await _emit(progress_cb, "downloading", 0.0, f"Mulai download direct ({info.kind})")

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(info.url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise DownloadError(f"HTTP {resp.status} dari {info.url}")

            total = int(resp.headers.get("Content-Length") or 0)
            if total and max_bytes and total > max_bytes:
                raise DownloadError(
                    f"Ukuran file ({human_bytes(total)}) melebihi limit "
                    f"{cfg.download.max_file_size_gb} GB"
                )

            filename = _filename_from_response(resp, info.url, title)
            target_ext = Path(filename).suffix or ".bin"
            target_path = ensure_unique_path(download_dir / f"{title}{target_ext}")
            tmp_path = temp_dir / (target_path.name + ".part")

            written = 0
            last_pct = -1
            try:
                with tmp_path.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        _check_cancel(cancel_event)
                        f.write(chunk)
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise DownloadError(
                                f"File melewati limit {cfg.download.max_file_size_gb} GB"
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
                                    f"Download {ipct}% ({human_bytes(written)}/{human_bytes(total)})",
                                )
                tmp_path.replace(target_path)
            except Exception:
                safe_unlink(tmp_path)
                raise

    size = target_path.stat().st_size
    _check_max_size(size, cfg, target_path)
    await _emit(
        progress_cb,
        "downloaded",
        100.0,
        f"Download selesai: {target_path.name} ({human_bytes(size)})",
    )
    return DownloadResult(
        path=target_path,
        size_bytes=size,
        source_kind=info.kind,
        tool_used="aiohttp",
    )


def _filename_from_response(resp: aiohttp.ClientResponse, url: str, fallback_title: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        m = _DISPOSITION_FILENAME_RE.search(cd)
        if m:
            cand = m.group(1) or m.group(2)
            if cand:
                cand = unquote(cand)
                cand = safe_filename(cand)
                if cand:
                    return cand
    parsed_path = unquote(urlparse(url).path)
    name = os.path.basename(parsed_path) or fallback_title or "download"
    return safe_filename(name)


# ----- yt-dlp --------------------------------------------------------------- #

async def _download_ytdlp(
    info: LinkInfo,
    title: str,
    cfg: AppConfig,
    progress_cb: ProgressCB,
    cancel_event: Optional[asyncio.Event],
) -> DownloadResult:
    download_dir = cfg.paths.download_dir
    download_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    last_pct = {"value": -1}

    def _hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadError("Job dibatalkan oleh user")
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = (done / total) * 100
                ipct = int(pct)
                if ipct != last_pct["value"] and ipct % 5 == 0:
                    last_pct["value"] = ipct
                    msg = f"yt-dlp {ipct}% ({human_bytes(done)}/{human_bytes(total)})"
                    asyncio.run_coroutine_threadsafe(
                        _emit(progress_cb, "downloading", pct, msg),
                        loop,
                    )
        elif status == "finished":
            asyncio.run_coroutine_threadsafe(
                _emit(progress_cb, "downloading", 100.0, "yt-dlp post-processing…"),
                loop,
            )

    outtmpl = str(download_dir / f"{title}.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_hook],
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "user_agent": cfg.download.user_agent,
        "noplaylist": True,
    }

    def _do() -> Path:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            raise DownloadError(f"yt-dlp tidak terinstall: {exc}") from exc
        try:
            with YoutubeDL(ydl_opts) as ydl:
                meta = ydl.extract_info(info.url, download=True)
                if isinstance(meta, dict) and "entries" in meta and meta["entries"]:
                    meta = meta["entries"][0]
                final_path = ydl.prepare_filename(meta)
                # post-merge can rewrite to .mp4
                p = Path(final_path)
                if not p.exists():
                    candidate = p.with_suffix(".mp4")
                    if candidate.exists():
                        p = candidate
                return p
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(f"yt-dlp error: {exc}") from exc

    await _emit(progress_cb, "downloading", 0.0, "Memulai yt-dlp…")
    final = await asyncio.to_thread(_do)
    _check_cancel(cancel_event)

    if not final.exists():
        raise DownloadError("yt-dlp selesai tapi file tidak ditemukan")

    size = final.stat().st_size
    _check_max_size(size, cfg, final)
    await _emit(
        progress_cb,
        "downloaded",
        100.0,
        f"yt-dlp selesai: {final.name} ({human_bytes(size)})",
    )
    return DownloadResult(
        path=final,
        size_bytes=size,
        source_kind="ytdlp",
        tool_used="yt-dlp",
    )


# ----- Helpers -------------------------------------------------------------- #

def _check_max_size(size: int, cfg: AppConfig, path: Path) -> None:
    max_bytes = cfg.download.max_file_size_gb * (1024 ** 3)
    if max_bytes and size > max_bytes:
        safe_unlink(path)
        raise DownloadError(
            f"File ({human_bytes(size)}) melebihi limit "
            f"{cfg.download.max_file_size_gb} GB"
        )


def is_uploadable_video(path: Path, cfg: AppConfig) -> bool:
    return is_video_extension(path.name, cfg.video_extensions)
