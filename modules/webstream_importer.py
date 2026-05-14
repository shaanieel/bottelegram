"""Auto insert uploaded Player4Me videos into ZAEIN webstream catalog.

The Drive web sends TMDB metadata and the selected Player4Me public domain to
the bot HTTP API. The bot keeps that metadata on the in-memory Job object, then
after Player4Me upload succeeds this helper inserts one row into the webstream
Supabase `films` table.

Required env on the RDP bot:
    SUPABASE_URL or WEBSTREAM_SUPABASE_URL
    SUPABASE_SERVICE_KEY or WEBSTREAM_SUPABASE_SERVICE_KEY

No browser secret is involved here; this runs only on the RDP/VPS bot.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .logger import get_logger
from .queue_manager import Job

log = get_logger(__name__)

ProgressFn = Callable[[str, Optional[float], str], Awaitable[None]]


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _normalize_domain(raw: Any) -> str:
    domain = str(raw or "").strip()
    if not domain:
        return ""
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    return domain


def _public_player_url(video_id: str, meta: dict[str, Any]) -> str:
    domain = _normalize_domain(
        meta.get("player_domain")
        or meta.get("domain")
        or meta.get("player4me_domain")
        or meta.get("selected_domain")
    )
    if domain and video_id:
        return f"https://{domain}/#{video_id}"
    return ""


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _row_from_job(job: Job, video_id: str) -> dict[str, Any] | None:
    meta = getattr(job, "stream_meta", None) or {}
    if not meta:
        return None

    tmdb = meta.get("tmdb") or {}
    kind = str(meta.get("kind") or tmdb.get("type") or "movie").lower()
    tipe = "series" if kind in {"series", "tv"} else "movie"

    title = str(tmdb.get("title") or meta.get("title") or job.title or "Untitled").strip()
    if not title:
        title = "Untitled"

    stream_url = job.embed_url or _public_player_url(video_id, meta)
    if not stream_url:
        # Fallback hanya dipakai jika Drive web belum mengirim domain. Normalnya
        # domain dipilih dari tabel player4me_domains, misalnya zaeinstore.qzz.io.
        stream_url = f"https://player4me.com/embed/{video_id}"

    row: dict[str, Any] = {
        "judul": title,
        "tipe": tipe,
        "drive_link": stream_url,
        "tahun": _as_int(tmdb.get("year")),
        "tmdb_id": str(tmdb.get("id") or "") or None,
        "tier": str(meta.get("tier") or "vip").lower(),
        "poster_url": tmdb.get("poster_url") or None,
        "backdrop_url": tmdb.get("backdrop_url") or None,
        "overview": tmdb.get("overview") or None,
        "genre": tmdb.get("genre") or None,
    }
    if tipe == "series":
        row["season"] = _as_int(meta.get("season")) or 1
        row["episode"] = _as_int(meta.get("episode")) or 1
    return row


async def maybe_insert_webstream(bot_app: Any, job: Job, video_id: str, progress: ProgressFn) -> None:
    """Insert uploaded item into Supabase films if stream_meta + env are present.

    This helper intentionally does not raise fatal exceptions. Upload success
    should remain success even if catalog insert needs manual retry.
    """
    row = _row_from_job(job, video_id)
    if not row:
        return

    supabase_url = _env("WEBSTREAM_SUPABASE_URL", "SUPABASE_URL")
    service_key = _env("WEBSTREAM_SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        await progress(
            "encoding",
            None,
            "Auto masuk webstream dilewati: SUPABASE_URL / SUPABASE_SERVICE_KEY belum diisi di .env bot.",
        )
        return

    url = supabase_url.rstrip("/") + "/rest/v1/films"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    await progress("encoding", 99.0, "Menyimpan film ke webstream/Supabase...")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(url, json=row, headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    await progress(
                        "encoding",
                        None,
                        f"Webstream insert gagal HTTP {resp.status}: {text[:180]}",
                    )
                    log.error("Webstream insert failed HTTP %s: %s", resp.status, text[:500])
                    return
                try:
                    data = await resp.json()
                except Exception:
                    data = None
                row_id = None
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    row_id = data[0].get("id")
                await bot_app.queue._update(
                    job,
                    embed_url=row.get("drive_link"),
                    progress_text=(
                        f"Player4Me selesai + masuk webstream"
                        + (f" (film_id={row_id})" if row_id else "")
                    ),
                )
                await progress("encoding", 100.0, "Film berhasil masuk webstream/Supabase.")
    except Exception as exc:
        log.exception("Webstream insert crashed")
        await progress("encoding", None, f"Webstream insert error: {exc}")
