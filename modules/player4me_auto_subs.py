"""Automatic Player4Me upload pipeline with sidecar + embedded subtitles.

Supported cases:
1. Google Drive folder containing video + .srt/.ass/.ssa/.vtt sidecar files.
2. Google Drive folder containing only video: extract embedded text subtitles.
3. Single mkv/mp4 link: extract embedded text subtitles when present.

The video is always uploaded first. Subtitles are uploaded only after the new
Player4Me video id is recovered from /video/manage, because the subtitle API is
PUT /video/manage/{video_id}/subtitle.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .downloader import download, is_uploadable_video
from .gdrive_api import GDriveAPIClient
from .logger import get_logger
from .player4me_uploader import Player4MeError, Player4MeUploadResult
from .queue_manager import Job, JobType
from .subtitle_extractor import (
    SUPPORTED_SUB_EXTENSIONS,
    detect_language_from_filename,
    extract_embedded_subtitles,
)
from .validators import classify_link, safe_filename

log = get_logger(__name__)

ProgressFn = Callable[[str, Optional[float], str], Awaitable[None]]
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
SUB_EXTS = set(SUPPORTED_SUB_EXTENSIONS) | {".sub"}


@dataclass
class SubtitleAsset:
    path: Path
    language: str
    name: str
    source: str


@dataclass
class PreparedMedia:
    video_path: Path
    video_size: int
    video_title: str
    subtitles: list[SubtitleAsset]
    cleanup_paths: list[Path]


LANGUAGE_ALIASES: dict[str, tuple[str, str]] = {
    "id": ("id", "Indonesian"), "ind": ("id", "Indonesian"),
    "ina": ("id", "Indonesian"), "indo": ("id", "Indonesian"),
    "indonesia": ("id", "Indonesian"), "indonesian": ("id", "Indonesian"),
    "bahasa": ("id", "Indonesian"), "bahasaindonesia": ("id", "Indonesian"),
    "en": ("en", "English"), "eng": ("en", "English"),
    "english": ("en", "English"), "inggris": ("en", "English"),
    "ar": ("ar", "Arabic"), "ara": ("ar", "Arabic"),
    "arabic": ("ar", "Arabic"), "arab": ("ar", "Arabic"),
    "ko": ("ko", "Korean"), "kor": ("ko", "Korean"),
    "korean": ("ko", "Korean"), "korea": ("ko", "Korean"),
    "ja": ("ja", "Japanese"), "jpn": ("ja", "Japanese"),
    "japanese": ("ja", "Japanese"), "japan": ("ja", "Japanese"), "jp": ("ja", "Japanese"),
    "zh": ("zh", "Chinese"), "chi": ("zh", "Chinese"), "zho": ("zh", "Chinese"),
    "chinese": ("zh", "Chinese"), "china": ("zh", "Chinese"), "cn": ("zh", "Chinese"),
    "ms": ("ms", "Malay"), "msa": ("ms", "Malay"), "may": ("ms", "Malay"),
    "malay": ("ms", "Malay"), "malaysia": ("ms", "Malay"),
    "th": ("th", "Thai"), "tha": ("th", "Thai"), "thai": ("th", "Thai"),
    "vi": ("vi", "Vietnamese"), "vie": ("vi", "Vietnamese"),
    "vietnamese": ("vi", "Vietnamese"), "vietnam": ("vi", "Vietnamese"),
    "es": ("es", "Spanish"), "spa": ("es", "Spanish"), "spanish": ("es", "Spanish"),
    "fr": ("fr", "French"), "fra": ("fr", "French"), "fre": ("fr", "French"), "french": ("fr", "French"),
    "de": ("de", "German"), "deu": ("de", "German"), "ger": ("de", "German"), "german": ("de", "German"),
    "it": ("it", "Italian"), "ita": ("it", "Italian"), "italian": ("it", "Italian"),
    "pt": ("pt", "Portuguese"), "por": ("pt", "Portuguese"), "portuguese": ("pt", "Portuguese"),
    "ru": ("ru", "Russian"), "rus": ("ru", "Russian"), "russian": ("ru", "Russian"),
    "hi": ("hi", "Hindi"), "hin": ("hi", "Hindi"), "hindi": ("hi", "Hindi"),
    "tr": ("tr", "Turkish"), "tur": ("tr", "Turkish"), "turkish": ("tr", "Turkish"),
}


def install_player4me_auto_subs(bot_app) -> None:
    """Patch QueueManager worker so Player4Me jobs use this pipeline."""
    if getattr(bot_app, "_player4me_auto_subs_installed", False):
        return
    bot_app._player4me_auto_subs_installed = True
    old_worker = bot_app.queue._worker

    async def wrapped(job: Job, cancel_event: asyncio.Event, progress: ProgressFn) -> None:
        if job.type in {
            JobType.UPLOAD_PLAYER4ME.value,
            JobType.UPLOAD_PLAYER4ME_SUBS.value,
            JobType.UPLOAD_PLAYER4ME_FOLDER.value,
        }:
            await run_player4me_auto(bot_app, job, cancel_event, progress)
            return
        await old_worker(job, cancel_event, progress)

    bot_app.queue._worker = wrapped
    log.info("Player4Me automatic subtitle pipeline installed")


async def run_player4me_auto(bot_app, job: Job, cancel_event: asyncio.Event, progress: ProgressFn) -> None:
    if not bot_app.player4me.is_configured():
        raise Player4MeError("Player4Me belum dikonfigurasi (PLAYER4ME_API_TOKEN kosong)")

    prepared = await _prepare_media(bot_app, job, cancel_event, progress)
    try:
        await bot_app.queue._update(
            job,
            file_path=str(prepared.video_path),
            file_size_bytes=prepared.video_size,
        )
        folder_id = bot_app.cfg.upload_targets.player4me_default_folder_id or None
        before_ids = await bot_app.player4me.snapshot_video_ids(folder_id=folder_id)
        await progress("uploading", 0.0, f"Upload video ke Player4Me: {prepared.video_path.name}")
        result = await bot_app.player4me.upload_local_file(
            prepared.video_path,
            prepared.video_title,
            folder_id=folder_id,
            progress_cb=progress,
            cancel_event=cancel_event,
        )
        video_id = await _resolve_video_id(bot_app, result, prepared, before_ids, folder_id)
        if not video_id:
            raise Player4MeError(
                "Upload video selesai tapi videoId belum ditemukan dari /video/manage. "
                "Subtitle tidak bisa dipasang tanpa videoId."
            )

        uploaded = 0
        if prepared.subtitles:
            await progress("encoding", 96.0, f"Upload {len(prepared.subtitles)} subtitle")
            for sub in prepared.subtitles:
                if cancel_event.is_set():
                    raise Player4MeError("Job dibatalkan oleh user")
                try:
                    await bot_app.player4me.upload_subtitle(
                        video_id,
                        sub.path,
                        language=sub.language,
                        name=sub.name,
                    )
                    uploaded += 1
                    await progress("encoding", None, f"Subtitle OK: {sub.path.name} ({sub.language})")
                except Exception as exc:
                    log.exception("Subtitle upload skipped: %s", sub.path)
                    await progress("encoding", None, f"Subtitle gagal dilewati: {sub.path.name} — {exc}")
        else:
            await progress("encoding", 98.0, "Tidak ada subtitle; lanjut selesai")

        await bot_app.queue._update(
            job,
            player4me_video_id=video_id,
            player4me_status="uploaded",
            player4me_engine="tus_auto_subs",
            progress=100.0,
            progress_text=f"Player4Me selesai: videoId={video_id}, subtitle={uploaded}",
        )
    finally:
        if bot_app.cfg.app.auto_delete_after_upload:
            _cleanup(prepared.cleanup_paths)


async def _prepare_media(bot_app, job: Job, cancel_event: asyncio.Event, progress: ProgressFn) -> PreparedMedia:
    try:
        info = classify_link(
            job.source_url,
            allow_google_drive=bot_app.cfg.download.allow_google_drive,
            allow_direct=bot_app.cfg.download.allow_direct_link,
            allow_ytdlp=bot_app.cfg.download.allow_ytdlp,
        )
    except Exception:
        info = None

    if info and info.kind == "google_drive_folder" and info.folder_id:
        return await _prepare_drive_folder(bot_app, job, info.folder_id, cancel_event, progress)

    dl = await download(job.source_url, job.title, bot_app.cfg, progress_cb=progress, cancel_event=cancel_event)
    if not is_uploadable_video(dl.path, bot_app.cfg):
        raise Player4MeError(f"File bukan video yang didukung: {dl.path.name}")
    subs = await _extract_embedded(bot_app, dl.path, progress)
    return PreparedMedia(dl.path, dl.size_bytes, job.title or dl.path.stem, subs, [dl.path, *[s.path for s in subs]])


async def _prepare_drive_folder(bot_app, job: Job, folder_id: str, cancel_event: asyncio.Event, progress: ProgressFn) -> PreparedMedia:
    client = GDriveAPIClient(bot_app.cfg)
    if not client.is_configured() or not (client.has_oauth_token() or client.has_service_account()):
        raise Player4MeError("Folder Drive butuh OAuth token atau Service Account")
    await progress("downloading", 0.0, "Membaca isi folder Drive")
    files = await client.list_folder(folder_id)
    videos = [f for f in files if Path(str(f.get("name") or "")).suffix.lower() in VIDEO_EXTS]
    subs = [f for f in files if Path(str(f.get("name") or "")).suffix.lower() in SUB_EXTS]
    if not videos:
        raise Player4MeError("Folder Drive tidak berisi file video mkv/mp4/mov/avi/webm")
    videos.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
    video = videos[0]
    video_name = str(video.get("name") or job.title or "video")
    title = Path(safe_filename(video_name)).stem or job.title
    target, size, _meta = await client.download(str(video["id"]), title, progress_cb=progress, cancel_event=cancel_event)
    subtitle_assets: list[SubtitleAsset] = []
    cleanup = [target]

    if subs:
        await progress("downloading", None, f"Download {len(subs)} sidecar subtitle")
        for sub in subs:
            sub_name = str(sub.get("name") or "subtitle.srt")
            sub_title = safe_filename(Path(sub_name).stem) or "subtitle"
            try:
                sub_path, _sub_size, _ = await client.download(str(sub["id"]), sub_title, progress_cb=progress, cancel_event=cancel_event)
                lang, label = _detect_language(sub_path.name, bot_app.cfg.upload_targets.player4me_default_subtitle_language)
                subtitle_assets.append(SubtitleAsset(sub_path, lang, label, "sidecar"))
                cleanup.append(sub_path)
            except Exception as exc:
                log.exception("Download sidecar subtitle failed: %s", sub_name)
                await progress("downloading", None, f"Subtitle sidecar dilewati: {sub_name} — {exc}")
    else:
        subtitle_assets = await _extract_embedded(bot_app, target, progress)
        cleanup.extend(s.path for s in subtitle_assets)

    return PreparedMedia(target, size, title, subtitle_assets, cleanup)


async def _extract_embedded(bot_app, video_path: Path, progress: ProgressFn) -> list[SubtitleAsset]:
    await progress("encoding", None, "Cek subtitle embed dengan ffprobe/ffmpeg")
    out_dir = bot_app.cfg.paths.temp_dir / f"subs-{int(time.time())}-{video_path.stem[:24]}"
    try:
        extracted = await extract_embedded_subtitles(
            video_path,
            out_dir,
            default_language=bot_app.cfg.upload_targets.player4me_default_subtitle_language,
            base_name=video_path.stem,
        )
    except Exception as exc:
        log.exception("Embedded subtitle extraction failed: %s", video_path)
        await progress("encoding", None, f"Cek subtitle gagal, upload video saja: {exc}")
        return []
    out: list[SubtitleAsset] = []
    for item in extracted:
        lang, label = _detect_language(item.path.name, item.language or "id")
        out.append(SubtitleAsset(item.path, lang, label or item.name, "embedded"))
    await progress("encoding", None, f"Subtitle embed ditemukan: {len(out)}")
    return out


async def _resolve_video_id(bot_app, result: Player4MeUploadResult, prepared: PreparedMedia, before_ids: set[str], folder_id: Optional[str]) -> Optional[str]:
    if result.primary_video_id:
        return result.primary_video_id
    for _ in range(12):
        found = await bot_app.player4me.find_recent_video_by_name(
            prepared.video_path.name,
            folder_id=folder_id,
            existing_ids=before_ids,
            max_pages=4,
        )
        if found and found.get("id"):
            return str(found["id"])
        await asyncio.sleep(5)
    return None


def _detect_language(filename: str, default: str) -> tuple[str, str]:
    raw = safe_filename(filename).lower()
    tokens = [t for t in raw.replace("_", ".").replace("-", ".").replace(" ", ".").split(".") if t]
    for token in reversed(tokens):
        cleaned = "".join(ch for ch in token if ch.isalpha())
        if cleaned in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[cleaned]
    lang, label = detect_language_from_filename(filename, default=default or "id")
    return lang, label


def _cleanup(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                safe_unlink(path)
        except Exception:
            pass
