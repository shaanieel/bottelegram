"""Filesystem helpers — stats, listing, deletion."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)


@dataclass
class StorageStats:
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        return 0.0 if self.total_bytes == 0 else (self.used_bytes / self.total_bytes) * 100


@dataclass
class FileInfo:
    name: str
    path: Path
    size_bytes: int


def disk_stats(path: str | os.PathLike[str]) -> StorageStats:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(str(p))
    return StorageStats(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
    )


def folder_size(path: str | os.PathLike[str]) -> int:
    """Recursive size of *path* in bytes (0 if missing)."""
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    for entry in p.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def list_files(path: str | os.PathLike[str]) -> list[FileInfo]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[FileInfo] = []
    for entry in sorted(p.iterdir()):
        if entry.name == ".gitkeep":
            continue
        try:
            if entry.is_file():
                out.append(
                    FileInfo(
                        name=entry.name,
                        path=entry,
                        size_bytes=entry.stat().st_size,
                    )
                )
        except OSError:
            continue
    return out


def delete_file(directory: str | os.PathLike[str], filename: str) -> Path:
    """Delete *directory/filename*. Refuses to traverse outside *directory*."""
    base = Path(directory).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base) + os.sep) and target != base:
        raise ValueError("Nama file tidak valid (ada path traversal).")
    if not target.exists():
        raise FileNotFoundError(f"File tidak ditemukan: {filename}")
    if target.is_dir():
        raise IsADirectoryError(f"{filename} adalah folder, bukan file.")
    target.unlink()
    log.info("Deleted file %s", target)
    return target


def clear_directory(path: str | os.PathLike[str]) -> int:
    """Delete every file (recursively) inside *path*. Returns count removed."""
    base = Path(path)
    if not base.exists():
        return 0
    removed = 0
    for entry in base.iterdir():
        if entry.name == ".gitkeep":
            continue
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
                removed += 1
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError as exc:
            log.warning("Failed to delete %s: %s", entry, exc)
    log.info("Cleared directory %s (%d entries removed)", base, removed)
    return removed


def safe_unlink(path: str | os.PathLike[str]) -> bool:
    """Delete *path* if it exists. Never raises. Returns True on success."""
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            p.unlink()
            return True
    except OSError as exc:
        log.warning("safe_unlink(%s) failed: %s", path, exc)
    return False


def human_bytes(num: int | float) -> str:
    """Format bytes as a human-readable string."""
    if num is None:
        return "?"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} EB"


def ensure_unique_path(target: Path) -> Path:
    """If *target* exists, append ``_1``, ``_2``, … until a free name is found."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
