"""Extract embedded subtitle streams from a video file using ffprobe + ffmpeg.

Used by the Player4Me "single file with embedded subs" flow:

1. Run ``ffprobe`` to enumerate every subtitle stream and grab the metadata
   we need (codec name and ``language`` tag).
2. For each *text-based* subtitle stream, run ``ffmpeg`` to copy or transcode
   it into a separate ``.srt`` (or ``.ass`` / ``.vtt``) file in the temp
   directory. Image-based subtitle codecs (PGS / VobSub / DVB) are skipped
   because they cannot be losslessly converted to text.
3. Return a list of :class:`ExtractedSubtitle` ready to be uploaded to
   Player4Me's ``PUT /api/v1/video/manage/{id}/subtitle`` endpoint.

Player4Me requires a 2-character ISO 639-1 language code for each subtitle.
ffprobe usually reports ISO 639-2 codes (eng / ind / jpn) so we map them down
in :func:`_normalize_language`. Anything we cannot map falls back to the bot
default (``id`` for Indonesian content).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logger import get_logger
from .validators import safe_filename

log = get_logger(__name__)


# ----- public types --------------------------------------------------------- #

@dataclass(frozen=True)
class ExtractedSubtitle:
    """One subtitle stream successfully written to a temp file."""

    path: Path
    language: str   # 2-char ISO 639-1 (always lower-case)
    name: str       # human-readable label (e.g. "English", "Indonesian")
    codec: str      # source codec name from ffprobe
    stream_index: int


class SubtitleExtractError(RuntimeError):
    """Raised when ffprobe / ffmpeg fails or produces no usable output."""


# ----- ISO 639-2 -> ISO 639-1 mapping (only the ones we are likely to see) -- #

_ISO_639_2_TO_1: dict[str, str] = {
    # Indonesian / Malay region (most relevant for the user)
    "ind": "id", "ind1": "id",
    "may": "ms", "msa": "ms",
    # English
    "eng": "en", "en": "en",
    # Common East Asian
    "jpn": "ja", "ja": "ja",
    "kor": "ko", "ko": "ko",
    "chi": "zh", "zho": "zh", "zh": "zh",
    # Common Western European
    "spa": "es", "es": "es",
    "fre": "fr", "fra": "fr", "fr": "fr",
    "ger": "de", "deu": "de", "de": "de",
    "ita": "it", "it": "it",
    "por": "pt", "pt": "pt",
    "nld": "nl", "dut": "nl", "nl": "nl",
    "swe": "sv", "sv": "sv",
    "nor": "no", "no": "no",
    "dan": "da", "da": "da",
    "fin": "fi", "fi": "fi",
    "pol": "pl", "pl": "pl",
    "rus": "ru", "ru": "ru",
    "tur": "tr", "tr": "tr",
    "ara": "ar", "ar": "ar",
    "hin": "hi", "hi": "hi",
    "tha": "th", "th": "th",
    "vie": "vi", "vi": "vi",
}

# Full names for common 2-char codes — used to label subs nicely in Player4Me.
_LANGUAGE_NAMES: dict[str, str] = {
    "id": "Indonesian",
    "ms": "Malay",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "ru": "Russian",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
}

# Subtitle codecs that are text-based and can be muxed into srt/ass/vtt.
_TEXT_CODECS = {
    "subrip", "srt",
    "ass", "ssa",
    "webvtt", "vtt",
    "mov_text",
    "text",
}

# Bitmap codecs we cannot convert to text losslessly — they are skipped.
_BITMAP_CODECS = {
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "dvb_subtitle",
    "xsub",
}

# File extensions Player4Me accepts on its subtitle endpoint.
SUPPORTED_SUB_EXTENSIONS: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt")


# ----- ffprobe / ffmpeg helpers -------------------------------------------- #

async def _run_command(cmd: list[str], *, timeout: float = 600.0) -> tuple[int, bytes, bytes]:
    """Run *cmd* and return ``(returncode, stdout, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SubtitleExtractError(
            f"Perintah ffmpeg/ffprobe timeout setelah {timeout:.0f}s: "
            f"{' '.join(cmd[:3])}…"
        )
    return proc.returncode or 0, stdout, stderr


async def _probe_streams(video_path: Path) -> list[dict]:
    """Return the list of subtitle streams in *video_path*."""
    if not video_path.exists():
        raise SubtitleExtractError(f"File tidak ditemukan: {video_path}")
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        str(video_path),
    ]
    rc, stdout, stderr = await _run_command(cmd, timeout=120.0)
    if rc != 0:
        raise SubtitleExtractError(
            f"ffprobe gagal (rc={rc}): {stderr.decode('utf-8', errors='replace')[:300]}"
        )
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SubtitleExtractError(
            f"ffprobe output bukan JSON valid: {stdout[:200]!r}"
        ) from exc
    streams = data.get("streams") or []
    return [s for s in streams if isinstance(s, dict)]


def _normalize_language(raw: Optional[str], default: str) -> str:
    """Map any language tag we got from ffprobe to a 2-char ISO 639-1 code."""
    if not raw:
        return default
    low = str(raw).strip().lower().replace("_", "-")
    # ffprobe sometimes reports "ind", "eng" — pure 3-char ISO 639-2.
    # Sometimes it reports BCP-47 like "en-US"; take the primary subtag.
    primary = low.split("-", 1)[0]
    if len(primary) == 2 and primary.isalpha():
        return primary
    if primary in _ISO_639_2_TO_1:
        return _ISO_639_2_TO_1[primary]
    return default


def _label_for(language: str, fallback_title: str) -> str:
    """Return a human-readable label for *language*, or *fallback_title*."""
    name = _LANGUAGE_NAMES.get(language)
    if name:
        return name
    if fallback_title:
        return fallback_title
    return language.upper() or "Subtitle"


def _output_extension(codec: str) -> str:
    """Pick an output extension that ffmpeg can mux *codec* into safely."""
    if codec in ("ass", "ssa"):
        return ".ass"
    if codec in ("webvtt", "vtt", "mov_text"):
        return ".vtt"
    # subrip / text / unknown text codec -> srt is the safest container.
    return ".srt"


# ----- public API ----------------------------------------------------------- #

async def extract_embedded_subtitles(
    video_path: str | Path,
    out_dir: str | Path,
    *,
    default_language: str = "id",
    base_name: Optional[str] = None,
) -> list[ExtractedSubtitle]:
    """Extract every text-based subtitle stream from *video_path*.

    Bitmap subtitle streams (PGS / VobSub / DVB) are skipped with a warning
    because they cannot be transcoded to a text format losslessly. Returns an
    empty list when there are no subtitle streams (or none are text-based) —
    that is *not* an error, the caller should just continue without subs.

    Parameters
    ----------
    default_language:
        2-char ISO 639-1 code used when ffprobe reports no language tag (or an
        unknown one). Defaults to ``"id"`` (Indonesian).
    base_name:
        Stem to use for output files. Defaults to ``video_path.stem``.
    """
    video = Path(video_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(base_name or video.stem) or "subtitle"

    streams = await _probe_streams(video)
    if not streams:
        log.info("Tidak ada subtitle stream di %s", video.name)
        return []

    extracted: list[ExtractedSubtitle] = []
    for s in streams:
        idx = int(s.get("index") or 0)
        codec = str(s.get("codec_name") or "").lower()
        tags = s.get("tags") or {}
        raw_lang = tags.get("language") if isinstance(tags, dict) else None
        raw_title = tags.get("title") if isinstance(tags, dict) else None
        if codec in _BITMAP_CODECS:
            log.warning(
                "Skip subtitle bitmap-only di %s (stream %d, codec=%s) — "
                "Player4Me hanya menerima text subtitles.",
                video.name, idx, codec,
            )
            continue
        if codec not in _TEXT_CODECS:
            log.warning(
                "Subtitle codec %s tidak dikenal di stream %d, dilewati.",
                codec, idx,
            )
            continue

        language = _normalize_language(raw_lang, default=default_language)
        label = _label_for(language, str(raw_title or ""))
        ext = _output_extension(codec)

        out_name = safe_filename(f"{stem}.{language}.{idx}{ext}")
        out_path = out / out_name

        # Subtitle streams use ``-map 0:<absolute_stream_index>``. ``-c:s`` lets
        # ffmpeg auto-pick the right text subtitle codec for the chosen output
        # container (srt / ass / vtt).
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel", "error",
            "-i", str(video),
            "-map", f"0:{idx}",
            "-c:s", _ffmpeg_codec_for(ext),
            str(out_path),
        ]
        rc, _stdout, stderr = await _run_command(cmd, timeout=600.0)
        if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            log.warning(
                "Ekstrak subtitle stream %d (%s -> %s) gagal: %s",
                idx, codec, ext,
                stderr.decode("utf-8", errors="replace")[:300],
            )
            # Make sure we don't leave a 0-byte file in temp.
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            continue

        extracted.append(
            ExtractedSubtitle(
                path=out_path,
                language=language,
                name=label,
                codec=codec,
                stream_index=idx,
            )
        )
        log.info(
            "Subtitle ter-extract: stream=%d codec=%s lang=%s file=%s",
            idx, codec, language, out_path.name,
        )

    return extracted


def _ffmpeg_codec_for(ext: str) -> str:
    """Pick the ``-c:s`` value that pairs with *ext* without re-encoding when possible."""
    if ext == ".ass":
        return "ass"
    if ext == ".vtt":
        return "webvtt"
    return "srt"


# ----- helpers for the "external sub file" flow ---------------------------- #

def detect_language_from_filename(filename: str, default: str = "id") -> tuple[str, str]:
    """Guess subtitle language + label from a filename like ``Movie.eng.srt``.

    Returns ``(language_2char, label)``. If nothing matches, returns
    ``(default, _LANGUAGE_NAMES[default] or default)``.
    """
    if not filename:
        return default, _LANGUAGE_NAMES.get(default, default.upper())

    stem = Path(filename).stem.lower()
    # Walk the dot-separated tags from right to left — the language tag is
    # almost always the last suffix before the extension (Movie.S01E01.eng.srt).
    parts = [p for p in stem.replace("_", ".").split(".") if p]
    for tag in reversed(parts):
        if len(tag) == 2 and tag.isalpha():
            return tag, _LANGUAGE_NAMES.get(tag, tag.upper())
        if tag in _ISO_639_2_TO_1:
            mapped = _ISO_639_2_TO_1[tag]
            return mapped, _LANGUAGE_NAMES.get(mapped, mapped.upper())
        # Common spelled-out names also appear in user filenames.
        spelled = {
            "indonesian": "id", "indonesia": "id",
            "malay": "ms",
            "english": "en",
            "japanese": "ja", "japan": "ja",
            "korean": "ko",
            "chinese": "zh",
            "spanish": "es",
            "french": "fr",
            "german": "de",
            "italian": "it",
            "portuguese": "pt",
        }
        if tag in spelled:
            mapped = spelled[tag]
            return mapped, _LANGUAGE_NAMES.get(mapped, mapped.upper())

    return default, _LANGUAGE_NAMES.get(default, default.upper())
