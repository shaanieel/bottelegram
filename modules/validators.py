"""Input/URL validators and safe-filename helpers.

These are intentionally string-only, no IO. Heavy work (HEAD requests etc.)
lives in :mod:`modules.downloader`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse


_GD_FILE_ID_RE = re.compile(
    r"(?:/d/|/file/d/|[?&](?:id|export=download&id)=)([A-Za-z0-9_-]{20,})"
)
_GD_FOLDER_ID_RE = re.compile(
    r"(?:/folders/|[?&]folderId=)([A-Za-z0-9_-]{20,})"
)
_DROPBOX_RE = re.compile(r"^https?://(?:www\.)?(?:dropbox|dl\.dropboxusercontent)\.com/", re.I)
_ONEDRIVE_RE = re.compile(r"^https?://(?:[^/]*\.)?(?:1drv\.ms|onedrive\.live\.com)/", re.I)
_GD_HOST_RE = re.compile(r"^https?://(?:[^/]*\.)?(?:drive|docs)\.google\.com/", re.I)


@dataclass(frozen=True)
class LinkInfo:
    # "google_drive" | "google_drive_folder" | "dropbox" | "onedrive" | "direct" | "ytdlp"
    kind: str
    url: str             # normalized URL ready to be passed to a downloader
    file_id: str | None = None
    folder_id: str | None = None


# ----- URL classification --------------------------------------------------- #

def is_url(text: str) -> bool:
    if not text:
        return False
    try:
        u = urlparse(text)
    except Exception:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def extract_google_drive_id(url: str) -> str | None:
    """Extract the file ID from any Google Drive URL we recognize."""
    if not url:
        return None
    m = _GD_FILE_ID_RE.search(url)
    if m:
        return m.group(1)
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        return None
    for key in ("id", "ids"):
        if key in q and q[key]:
            return q[key][0]
    return None


def extract_google_drive_folder_id(url: str) -> str | None:
    """Extract the folder ID from a Drive URL like
    ``https://drive.google.com/drive/folders/<ID>?usp=sharing``."""
    if not url:
        return None
    m = _GD_FOLDER_ID_RE.search(url)
    if m:
        return m.group(1)
    return None


def is_google_drive_folder(url: str) -> bool:
    return bool(_GD_HOST_RE.match(url or "")) and extract_google_drive_folder_id(url or "") is not None


def is_google_drive(url: str) -> bool:
    if not url:
        return False
    if is_google_drive_folder(url):
        # Folder URLs are still Drive but classified separately.
        return False
    return bool(_GD_HOST_RE.match(url)) or extract_google_drive_id(url) is not None


def is_dropbox(url: str) -> bool:
    return bool(_DROPBOX_RE.match(url or ""))


def is_onedrive(url: str) -> bool:
    return bool(_ONEDRIVE_RE.match(url or ""))


def normalize_dropbox(url: str) -> str:
    """Force Dropbox direct download (?dl=1)."""
    if not url:
        return url
    if "dl=0" in url:
        url = url.replace("dl=0", "dl=1")
    elif "dl=1" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}dl=1"
    return url


def normalize_onedrive(url: str) -> str:
    """Best-effort OneDrive direct-download URL."""
    if not url:
        return url
    if "download=1" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}download=1"


def classify_link(
    url: str,
    *,
    allow_google_drive: bool = True,
    allow_direct: bool = True,
    allow_ytdlp: bool = True,
) -> LinkInfo:
    """Classify *url* into one of the supported link kinds."""
    if not is_url(url):
        raise ValueError("URL tidak valid (harus diawali http:// atau https://)")

    if allow_google_drive and is_google_drive_folder(url):
        folder_id = extract_google_drive_folder_id(url) or ""
        return LinkInfo(
            kind="google_drive_folder",
            url=url,
            folder_id=folder_id or None,
        )

    if allow_google_drive and is_google_drive(url):
        fid = extract_google_drive_id(url) or ""
        canonical = (
            f"https://drive.google.com/uc?id={fid}&export=download"
            if fid
            else url
        )
        return LinkInfo(kind="google_drive", url=canonical, file_id=fid or None)

    if allow_direct and is_dropbox(url):
        return LinkInfo(kind="dropbox", url=normalize_dropbox(url))

    if allow_direct and is_onedrive(url):
        return LinkInfo(kind="onedrive", url=normalize_onedrive(url))

    if allow_direct and _looks_like_direct_file(url):
        return LinkInfo(kind="direct", url=url)

    if allow_ytdlp:
        return LinkInfo(kind="ytdlp", url=url)

    return LinkInfo(kind="direct", url=url)


def _looks_like_direct_file(url: str) -> bool:
    try:
        path = urlparse(url).path
    except Exception:
        return False
    lower = path.lower()
    direct_exts = (
        ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
        ".mp3", ".m4a", ".wav", ".flac", ".ogg",
        ".zip", ".rar", ".7z", ".tar", ".gz",
        ".pdf", ".jpg", ".jpeg", ".png",
    )
    return any(lower.endswith(ext) for ext in direct_exts)


# ----- Filename / title sanitation ----------------------------------------- #

_INVALID_FN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, default: str = "file", max_len: int = 180) -> str:
    """Return a cross-platform safe filename derived from *name*."""
    if not name:
        return default
    name = unicodedata.normalize("NFKC", name).strip()
    name = _INVALID_FN_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return default
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 8 and stem:
        if stem.upper() in _RESERVED_WIN:
            stem = f"_{stem}"
        truncated = stem[: max_len - len(ext) - 1]
        return f"{truncated}.{ext}"
    if name.upper() in _RESERVED_WIN:
        name = f"_{name}"
    return name[:max_len]


def parse_command_args(text: str) -> tuple[str, str | None]:
    """Parse ``Title | URL`` style command arguments.

    Returns ``(title, url_or_none)``. If only one part is present we treat it as
    a URL (when it looks like one) or as a title.
    """
    if not text:
        return "", None
    parts = [p.strip() for p in text.split("|", maxsplit=1)]
    if len(parts) == 2:
        title, url = parts
        return title, (url or None)
    only = parts[0]
    if is_url(only):
        return "", only
    return only, None


def is_video_extension(filename: str, allowed: Iterable[str]) -> bool:
    name = (filename or "").lower()
    return any(name.endswith(ext.lower()) for ext in allowed)


def parse_size_to_bytes(text: str) -> int | None:
    """Parse strings like ``'2 GB'`` or ``'500MB'`` into bytes."""
    if not text:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?B)\s*$", text.strip(), re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(val * mult)


def looks_like_video_url(url: str) -> bool:
    """Heuristic: does the URL look like it points at a downloadable video?"""
    try:
        path = urlparse(url).path
    except Exception:
        return False
    lower = unquote(path).lower()
    return any(
        lower.endswith(ext)
        for ext in (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
    )
