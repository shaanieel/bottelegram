"""Telegram Index downloader.

Scrape daftar file dari halaman HTML Telegram Index
(contoh: https://zindex.aioarea.us.kg/zEu6c) dan download file-file yang
cocok dengan filter keyword.

Format URL download dari index:
  https://<host>/<channel_code>/<file_id>/<filename>

File diurutkan berdasarkan urutan di HTML (terbaru dulu, sesuai tampilan index).

Auth
----
Banyak instance Telegram Index sekarang **wajib login** sebelum bisa fetch
halaman index dan men-download file. Bot ini support login otomatis lewat
:class:`TGIndexCredentials` (username + password) yang dibaca dari .env
(``TGINDEX_USERNAME`` / ``TGINDEX_PASSWORD``). Sekali login, cookie session
``TG_INDEX_SESSION`` di-cache di :class:`TGIndexClient` sampai expire / 401 /
redirect lagi ke ``/login`` — bot otomatis re-login saat itu juga.
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

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ZAEINBot/1.0)"
LOGIN_PATH = "/login"


class TGIndexError(RuntimeError):
    """Raised on any Telegram Index scraping/download failure."""


class TGIndexAuthError(TGIndexError):
    """Raised when authentication is required but credentials are missing/invalid."""


@dataclass
class TGIndexFile:
    """Satu entry file dari Telegram Index."""
    filename: str
    download_url: str
    size_text: str       # contoh: "1.05 GiB"
    file_type: str       # contoh: "VIDEO"
    position: int        # urutan di halaman (0 = paling atas / terbaru)


@dataclass
class TGIndexCredentials:
    """Username + password untuk auto-login ke halaman Telegram Index."""
    username: str
    password: str

    def is_set(self) -> bool:
        return bool(self.username and self.password)


# ----- Client (handles login + cookies) ------------------------------------ #


class TGIndexClient:
    """HTTP client dengan auto-login + cookie caching untuk Telegram Index.

    Pakai pattern::

        client = TGIndexClient(credentials=TGIndexCredentials("user", "pass"))
        files = await client.scrape_index("https://zindex.aioarea.us.kg/zEu6c")
        for f in files:
            await client.download_file(f, downloads_dir, temp_dir, cfg=cfg)
    """

    def __init__(
        self,
        *,
        credentials: Optional[TGIndexCredentials] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 30,
    ) -> None:
        self._credentials = credentials
        self._user_agent = user_agent
        self._timeout = timeout
        # cookie cache keyed by site origin (scheme://netloc)
        self._cookies: dict[str, dict[str, str]] = {}

    # ---- public ------------------------------------------------------------ #

    @property
    def has_credentials(self) -> bool:
        return self._credentials is not None and self._credentials.is_set()

    def cookies_for(self, base_url: str) -> dict[str, str]:
        return dict(self._cookies.get(_origin_of(base_url), {}))

    def reset_cookies(self, base_url: Optional[str] = None) -> None:
        if base_url is None:
            self._cookies.clear()
        else:
            self._cookies.pop(_origin_of(base_url), None)

    async def login(self, base_url: str) -> dict[str, str]:
        """Login ke instance index dan kembalikan cookies session.

        ``base_url`` cukup origin (mis. ``https://zindex.aioarea.us.kg``) atau
        URL halaman lain di domain yang sama — kita ekstrak origin-nya.
        """
        if not self.has_credentials:
            raise TGIndexAuthError(
                "Telegram Index butuh login tapi TGINDEX_USERNAME / "
                "TGINDEX_PASSWORD belum di-set di .env."
            )
        assert self._credentials is not None  # narrowed by has_credentials

        origin = _origin_of(base_url)
        login_url = origin + LOGIN_PATH

        data = {
            "username": self._credentials.username,
            "password": self._credentials.password,
            "remember": "true",
            "redirect_to": "",
        }
        headers = {"User-Agent": self._user_agent}
        req_timeout = aiohttp.ClientTimeout(total=self._timeout)

        log.info("TGIndex login → %s (user=%s)", login_url, self._credentials.username)
        async with aiohttp.ClientSession(
            timeout=req_timeout, headers=headers
        ) as session:
            async with session.post(
                login_url, data=data, allow_redirects=False
            ) as resp:
                # Login form submits redirect (302/303) on success. 200 means
                # the form re-rendered with an error.
                if resp.status not in (301, 302, 303, 307, 308):
                    body = await resp.text()
                    snippet = _strip_html_text(body)[:200]
                    raise TGIndexAuthError(
                        f"Login gagal (HTTP {resp.status}). Cek username/"
                        f"password. Detail: {snippet!r}"
                    )

                cookies = {c.key: c.value for c in session.cookie_jar}
                if not cookies:
                    raise TGIndexAuthError(
                        "Login berhasil redirect tapi tidak ada cookie "
                        "session di-set."
                    )

        self._cookies[origin] = cookies
        log.info(
            "TGIndex login sukses (origin=%s, %d cookies cached)",
            origin, len(cookies),
        )
        return cookies

    async def scrape_index(
        self,
        index_url: str,
        *,
        keyword_filter: Optional[str] = None,
    ) -> list[TGIndexFile]:
        """Scrape halaman index → daftar :class:`TGIndexFile`."""
        html_content = await self._get_text(index_url)
        return _parse_index_html(html_content, index_url, keyword_filter)

    async def download_file(
        self,
        tg_file: TGIndexFile,
        download_dir: Path,
        temp_dir: Path,
        *,
        cfg: AppConfig,
        progress_cb: ProgressCB = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Path:
        """Download satu file dari Telegram Index ke ``download_dir``."""
        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        safe_name = safe_filename(tg_file.filename) or "download"
        target_path = ensure_unique_path(download_dir / safe_name)
        tmp_path = temp_dir / (target_path.name + ".part")

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
        chunk_size = max(64 * 1024, cfg.download.chunk_size_bytes)
        max_bytes = cfg.download.max_file_size_gb * (1024 ** 3)

        await _emit(
            progress_cb, "downloading", 0.0,
            f"Download dari TG Index: {tg_file.filename} ({tg_file.size_text})",
        )

        # First attempt with current cookies; on auth failure re-login once.
        for attempt in (1, 2):
            cookies = await self._ensure_cookies(tg_file.download_url)
            headers = {"User-Agent": self._user_agent}
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers, cookies=cookies
            ) as session:
                async with session.get(
                    tg_file.download_url, allow_redirects=False
                ) as resp:
                    # If redirected to /login, our session expired.
                    if _is_login_redirect(resp):
                        if attempt == 1 and self.has_credentials:
                            log.info(
                                "TGIndex download redirect ke /login, "
                                "reset cookies dan retry…"
                            )
                            self.reset_cookies(tg_file.download_url)
                            continue
                        raise TGIndexAuthError(
                            "Server me-redirect ke /login saat download. "
                            "Login gagal atau session habis."
                        )

                    if resp.status in (301, 302, 303, 307, 308):
                        # Some servers redirect to a CDN — re-issue with redirects allowed.
                        return await self._download_with_redirects(
                            tg_file, target_path, tmp_path,
                            chunk_size=chunk_size, max_bytes=max_bytes,
                            cfg=cfg, progress_cb=progress_cb,
                            cancel_event=cancel_event,
                        )

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
                                        f"File melewati limit "
                                        f"{cfg.download.max_file_size_gb} GB"
                                    )
                                if total:
                                    pct = (written / total) * 100
                                    ipct = int(pct)
                                    if ipct != last_pct and ipct % 5 == 0:
                                        last_pct = ipct
                                        await _emit(
                                            progress_cb, "downloading", pct,
                                            f"Download {ipct}% "
                                            f"({human_bytes(written)}/{human_bytes(total)})",
                                        )
                        tmp_path.replace(target_path)
                    except Exception:
                        safe_unlink(tmp_path)
                        raise

            # If we get here, download succeeded.
            break

        size = target_path.stat().st_size
        await _emit(
            progress_cb, "downloaded", 100.0,
            f"Download selesai: {target_path.name} ({human_bytes(size)})",
        )
        log.info("TGIndex download selesai: %s (%s)", target_path.name, human_bytes(size))
        return target_path

    # ---- internal --------------------------------------------------------- #

    async def _ensure_cookies(self, url: str) -> dict[str, str]:
        """Return cookies for the URL's origin, logging in if needed."""
        origin = _origin_of(url)
        cookies = self._cookies.get(origin)
        if cookies:
            return cookies
        if not self.has_credentials:
            return {}
        await self.login(url)
        return self._cookies.get(origin, {})

    async def _get_text(self, url: str) -> str:
        """GET URL with auto-login on /login redirect (1 retry)."""
        headers = {"User-Agent": self._user_agent}
        req_timeout = aiohttp.ClientTimeout(total=self._timeout)

        for attempt in (1, 2):
            cookies = await self._ensure_cookies(url)
            async with aiohttp.ClientSession(
                timeout=req_timeout, headers=headers, cookies=cookies
            ) as session:
                async with session.get(url, allow_redirects=False) as resp:
                    if _is_login_redirect(resp):
                        if attempt == 1 and self.has_credentials:
                            log.info(
                                "TGIndex GET redirect ke /login, "
                                "reset cookies dan retry…"
                            )
                            self.reset_cookies(url)
                            continue
                        raise TGIndexAuthError(
                            f"Halaman {url} memerlukan login (redirect ke /login). "
                            "Set TGINDEX_USERNAME / TGINDEX_PASSWORD di .env."
                        )

                    if resp.status in (301, 302, 303, 307, 308):
                        # Follow non-login redirects manually so we keep cookies.
                        loc = resp.headers.get("Location") or ""
                        if not loc:
                            raise TGIndexError(
                                f"Redirect tanpa Location dari {url}"
                            )
                            # unreachable
                        next_url = urljoin(url, loc)
                        return await self._get_text(next_url)

                    if resp.status >= 400:
                        raise TGIndexError(
                            f"Gagal fetch {url}: HTTP {resp.status}"
                        )
                    return await resp.text()

        # Should not reach here.
        raise TGIndexError(f"Gagal fetch {url}: retry exhausted")

    async def _download_with_redirects(
        self,
        tg_file: TGIndexFile,
        target_path: Path,
        tmp_path: Path,
        *,
        chunk_size: int,
        max_bytes: int,
        cfg: AppConfig,
        progress_cb: ProgressCB,
        cancel_event: Optional[asyncio.Event],
    ) -> Path:
        """Fallback download path that follows non-login redirects automatically."""
        cookies = self.cookies_for(tg_file.download_url)
        headers = {"User-Agent": self._user_agent}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers, cookies=cookies
        ) as session:
            async with session.get(
                tg_file.download_url, allow_redirects=True
            ) as resp:
                if str(resp.url).rstrip("/").endswith(LOGIN_PATH):
                    raise TGIndexAuthError(
                        "Download follow-redirects mendarat di /login. "
                        "Login gagal atau session habis."
                    )
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
                                    f"File melewati limit "
                                    f"{cfg.download.max_file_size_gb} GB"
                                )
                            if total:
                                pct = (written / total) * 100
                                ipct = int(pct)
                                if ipct != last_pct and ipct % 5 == 0:
                                    last_pct = ipct
                                    await _emit(
                                        progress_cb, "downloading", pct,
                                        f"Download {ipct}% "
                                        f"({human_bytes(written)}/{human_bytes(total)})",
                                    )
                    tmp_path.replace(target_path)
                except Exception:
                    safe_unlink(tmp_path)
                    raise

        return target_path


# ----- Module-level convenience wrappers (back-compat) --------------------- #
# Keep the old API so nothing else has to change wholesale.


async def scrape_index(
    index_url: str,
    *,
    keyword_filter: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: int = 30,
    credentials: Optional[TGIndexCredentials] = None,
    client: Optional[TGIndexClient] = None,
) -> list[TGIndexFile]:
    """Scrape Telegram Index dan kembalikan daftar file.

    Parameters
    ----------
    index_url:
        URL halaman index, contoh: ``https://zindex.aioarea.us.kg/zEu6c``.
    keyword_filter:
        Kalau diisi, hanya kembalikan file yang nama filenya mengandung
        string ini (case-insensitive). Contoh: ``"vmx"``.
    credentials:
        Optional username/password untuk login otomatis ketika halaman
        memerlukan auth. Kalau ``client`` di-pass, parameter ini diabaikan.
    client:
        Optional :class:`TGIndexClient` yang sudah dikonfigurasi (mis. dengan
        cookie cache). Lebih efisien untuk multiple call berturut-turut.
    """
    if client is None:
        client = TGIndexClient(
            credentials=credentials, user_agent=user_agent, timeout=timeout,
        )
    return await client.scrape_index(index_url, keyword_filter=keyword_filter)


async def download_tgindex_file(
    tg_file: TGIndexFile,
    download_dir: Path,
    temp_dir: Path,
    *,
    cfg: AppConfig,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[asyncio.Event] = None,
    credentials: Optional[TGIndexCredentials] = None,
    client: Optional[TGIndexClient] = None,
) -> Path:
    """Download satu file dari Telegram Index ke ``download_dir``."""
    if client is None:
        client = TGIndexClient(
            credentials=credentials,
            user_agent=cfg.download.user_agent,
        )
    return await client.download_file(
        tg_file,
        download_dir,
        temp_dir,
        cfg=cfg,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )


# ----- HTML parser --------------------------------------------------------- #


def _parse_index_html(
    html_content: str,
    base_url: str,
    keyword_filter: Optional[str],
) -> list[TGIndexFile]:
    """Parse HTML dan extract daftar file."""
    soup = BeautifulSoup(html_content, "html.parser")
    files: list[TGIndexFile] = []

    # Heuristic: kalau halaman mengandung form login, ini bukan index list
    # (server biasanya 200-OK render form login alih-alih redirect).
    if soup.select_one('form[action="/login"]'):
        raise TGIndexAuthError(
            f"Halaman {base_url} adalah form login. "
            "Set TGINDEX_USERNAME / TGINDEX_PASSWORD di .env."
        )

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
        len(files), keyword_filter,
    )
    return files


# ----- Helpers ------------------------------------------------------------- #


def _origin_of(url: str) -> str:
    """Return ``scheme://netloc`` of ``url`` for cookie-jar keying."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    return f"{p.scheme}://{p.netloc}"


def _is_login_redirect(resp: aiohttp.ClientResponse) -> bool:
    if resp.status not in (301, 302, 303, 307, 308):
        return False
    loc = resp.headers.get("Location") or ""
    return loc.endswith(LOGIN_PATH) or LOGIN_PATH + "?" in loc


def _strip_html_text(html_content: str) -> str:
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        return " ".join(soup.get_text(" ", strip=True).split())
    except Exception:
        return html_content


async def _emit(cb: ProgressCB, stage: str, percent: Optional[float], message: str) -> None:
    if cb is None:
        return
    try:
        result = cb(stage, percent, message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        log.debug("progress_cb raised %s", exc)


# Re-exports for type checkers / star-imports.
__all__ = [
    "TGIndexError",
    "TGIndexAuthError",
    "TGIndexFile",
    "TGIndexCredentials",
    "TGIndexClient",
    "scrape_index",
    "download_tgindex_file",
]
