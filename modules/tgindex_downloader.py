"""Telegram Index downloader.

Scrape daftar file dari halaman HTML Telegram Index
(contoh: https://zindex.aioarea.us.kg/zEu6c) dan download file-file yang
cocok dengan filter keyword.

Format URL download dari index:
  https://<host>/<channel_code>/<file_id>/<filename>

File diurutkan berdasarkan urutan di HTML (terbaru dulu, sesuai tampilan index).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .config_manager import AppConfig
from .logger import get_logger
from .storage_manager import ensure_unique_path, human_bytes, safe_unlink
from .validators import safe_filename

log = get_logger(__name__)

ProgressCB = Optional[Callable[[str, Optional[float], str], Awaitable[None]]]


class TGIndexError(RuntimeError):
    """Raised on any Telegram Index scraping/download failure."""


@dataclass
class TGIndexFile:
    """Satu entry file dari Telegram Index."""
    filename: str
    download_url: str
    size_text: str       # contoh: "1.05 GiB"
    file_type: str       # contoh: "VIDEO"
    position: int        # urutan di halaman (0 = paling atas / terbaru)


async def scrape_index(
    index_url: str,
    *,
    keyword_filter: Optional[str] = None,
    user_agent: str = "Mozilla/5.0 (compatible; ZAEINBot/1.0)",
    timeout: int = 30,
) -> list[TGIndexFile]:
    """Scrape Telegram Index dan kembalikan daftar file.

    Parameters
    ----------
    index_url:
        URL halaman index, contoh: https://zindex.aioarea.us.kg/zEu6c
    keyword_filter:
        Kalau diisi, hanya kembalikan file yang nama filenya mengandung
        string ini (case-insensitive). Contoh: "vmx", "1080p", "WEB-DL".
    """
    headers = {"User-Agent": user_agent}
    req_timeout = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(timeout=req_timeout, headers=headers) as session:
        async with session.get(index_url) as resp:
            if resp.status >= 400:
                raise TGIndexError(
                    f"Gagal fetch index (HTTP {resp.status}): {index_url}"
                )
            html_content = await resp.text()

    return _parse_index_html(html_content, index_url, keyword_filter)


def _parse_index_html(
    html_content: str,
    base_url: str,
    keyword_filter: Optional[str],
) -> list[TGIndexFile]:
    """Parse HTML dan extract daftar file."""
    soup = BeautifulSoup(html_content, "html.parser")
    files: list[TGIndexFile] = []

    # Setiap file ada dalam div.modern-card
    cards = soup.select("div.modern-card")

    for pos, card in enumerate(cards):
        # Ambil nama file dari alt attribute img atau dari link teks
        filename = ""
        img = card.select_one("img[alt]")
        if img and img.get("alt"):
            filename = str(img["alt"]).strip()

        if not filename:
            # Fallback: ambil dari teks link di bawah card
            link_el = card.select_one("div a.block")
            if link_el:
                filename = link_el.get_text(strip=True)

        if not filename:
            continue

        # Cari link download — pattern: href ending dengan /<filename>
        # (bukan /view)
        download_url = ""
        for a in card.select("a[href]"):
            href = str(a.get("href", ""))
            # Skip link /view
            if href.endswith("/view"):
                continue
            # Link download biasanya: /<channel>/<id>/<filename>
            if "/" in href and not href.endswith("/view"):
                # Resolve relative URL
                full = urljoin(base_url, href)
                # Pastikan URL mengandung nama file (bukan hanya path pendek)
                path_parts = urlparse(full).path.rstrip("/").split("/")
                if len(path_parts) >= 3:
                    download_url = full
                    break

        # Fallback: cari di onclick attribute (M3U playlist button)
        if not download_url:
            for el in card.select("[onclick]"):
                onclick = str(el.get("onclick", ""))
                # Contoh: singleItemPlaylist('zEu6c/5125/filename.mkv', ...)
                m = re.search(r"singleItemPlaylist\('([^']+)'", onclick)
                if m:
                    path = m.group(1)
                    parsed = urlparse(base_url)
                    download_url = f"{parsed.scheme}://{parsed.netloc}/{path}"
                    break

        if not download_url:
            log.debug("Skip: tidak ada download URL untuk %s", filename)
            continue

        # Size badge
        size_text = ""
        size_el = card.select_one(".badge-size")
        if size_el:
            size_text = size_el.get_text(strip=True)

        # Type badge (VIDEO, DOCUMENT, dll)
        file_type = ""
        type_el = card.select_one(".badge-type")
        if type_el:
            file_type = type_el.get_text(strip=True)

        # Apply keyword filter
        if keyword_filter:
            if keyword_filter.lower() not in filename.lower():
                continue

        files.append(TGIndexFile(
            filename=filename,
            download_url=download_url,
            size_text=size_text,
            file_type=file_type,
            position=pos,
        ))

    log.info(
        "Scrape index selesai: %d file ditemukan (filter=%r)",
        len(files), keyword_filter
    )
    return files


async def download_tgindex_file(
    tg_file: TGIndexFile,
    download_dir: Path,
    temp_dir: Path,
    *,
    cfg: AppConfig,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Path:
    """Download satu file dari Telegram Index ke download_dir.

    Returns path ke file yang sudah di-download.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    safe_name = safe_filename(tg_file.filename) or "download"
    target_path = ensure_unique_path(download_dir / safe_name)
    tmp_path = temp_dir / (target_path.name + ".part")

    headers = {"User-Agent": cfg.download.user_agent}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    chunk_size = max(64 * 1024, cfg.download.chunk_size_bytes)
    max_bytes = cfg.download.max_file_size_gb * (1024 ** 3)

    await _emit(
        progress_cb, "downloading", 0.0,
        f"Download dari TG Index: {tg_file.filename} ({tg_file.size_text})"
    )

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(tg_file.download_url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise TGIndexError(
                    f"HTTP {resp.status} saat download {tg_file.filename}"
                )

            total = int(resp.headers.get("Content-Length") or 0)
            if total and max_bytes and total > max_bytes:
                raise TGIndexError(
                    f"File ({human_bytes(total)}) melebihi limit "
                    f"{cfg.download.max_file_size_gb} GB"
                )

            written = 0
            last_pct = -1
            try:
                with tmp_path.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        if cancel_event is not None and cancel_event.is_set():
                            raise TGIndexError("Job dibatalkan oleh user")
                        f.write(chunk)
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise TGIndexError(
                                f"File melewati limit {cfg.download.max_file_size_gb} GB"
                            )
                        if total:
                            pct = (written / total) * 100
                            ipct = int(pct)
                            if ipct != last_pct and ipct % 5 == 0:
                                last_pct = ipct
                                await _emit(
                                    progress_cb, "downloading", pct,
                                    f"Download {ipct}% "
                                    f"({human_bytes(written)}/{human_bytes(total)})"
                                )
                tmp_path.replace(target_path)
            except Exception:
                safe_unlink(tmp_path)
                raise

    size = target_path.stat().st_size
    await _emit(
        progress_cb, "downloaded", 100.0,
        f"Download selesai: {target_path.name} ({human_bytes(size)})"
    )
    log.info("TGIndex download selesai: %s (%s)", target_path.name, human_bytes(size))
    return target_path


async def _emit(cb: ProgressCB, stage: str, percent: Optional[float], message: str) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        log.debug("progress_cb raised %s", exc)
