"""Small HTTP API so ZAEIN Drive web can enqueue bot jobs.

This module exposes a local aiohttp server beside the Telegram polling bot.
The server is intended to run on the RDP/VPS and be exposed through
Cloudflare Tunnel.

Env variables:
    BOT_API_SECRET   required for protected endpoints
    BOT_API_HOST     default: 127.0.0.1
    BOT_API_PORT     default: 8787
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .logger import get_logger
from .queue_manager import Job, JobStatus, JobType
from .validators import safe_filename

log = get_logger(__name__)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Zaein-Secret, X-Bot-Api-Secret",
}


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=CORS_HEADERS)


def _error(status: int, message: str) -> web.Response:
    return _json({"ok": False, "error": message}, status=status)


def _drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view?usp=drive_link"


def _drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}?usp=drive_link"


def _human_title(raw: str) -> str:
    name = safe_filename(raw or "Untitled")
    stem = Path(name).stem or name or "Untitled"
    return stem.replace("_", " ").strip() or "Untitled"


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.job_id,
        "type": job.type,
        "title": job.title,
        "source_url": job.source_url,
        "status": job.status,
        "stage": job.progress_text or job.status,
        "progress": round(float(job.progress or 0), 2),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.finished_at or job.started_at or job.created_at,
        "error_message": job.error_message,
        "file_size_bytes": job.file_size_bytes,
        "player4me_video_id": job.player4me_video_id,
        "player4me_status": job.player4me_status,
        "player4me_engine": job.player4me_engine,
        "gdrive_file_id": job.gdrive_file_id,
        "gdrive_web_link": job.gdrive_web_link,
        "embed_url": job.embed_url,
    }


class BotHttpApi:
    def __init__(self, bot_app) -> None:
        self.bot_app = bot_app
        self.cfg = bot_app.cfg
        self.secret = os.getenv("BOT_API_SECRET", "").strip()
        self.host = os.getenv("BOT_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
        self.port = int(os.getenv("BOT_API_PORT", "8787") or "8787")
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        if self.runner is not None:
            return
        app = web.Application(middlewares=[self._cors_middleware, self._auth_middleware])
        app.add_routes([
            web.options("/{tail:.*}", self.options),
            web.get("/api/health", self.health),
            web.get("/api/jobs", self.list_jobs),
            web.get("/api/jobs/{job_id}", self.get_job),
            web.post("/api/jobs/{job_id}/cancel", self.cancel_job),
            web.post("/api/jobs/movie", self.create_movie_job),
            web.post("/api/jobs/series", self.create_series_jobs),
        ])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        log.info("Bot HTTP API listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=CORS_HEADERS)
        resp = await handler(request)
        for k, v in CORS_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS" or request.path == "/api/health":
            return await handler(request)
        if not self.secret:
            return _error(503, "BOT_API_SECRET belum diisi di .env bot")
        got = (
            request.headers.get("X-Zaein-Secret")
            or request.headers.get("X-Bot-Api-Secret")
            or request.headers.get("Authorization", "").replace("Bearer ", "", 1)
        ).strip()
        if got != self.secret:
            return _error(401, "secret salah atau kosong")
        return await handler(request)

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CORS_HEADERS)

    async def health(self, request: web.Request) -> web.Response:
        q = self.bot_app.queue
        return _json({
            "ok": True,
            "name": self.cfg.app.name,
            "time": time.time(),
            "secret_set": bool(self.secret),
            "queued": len(q.queued_jobs()),
            "active": len(q.active_jobs()),
            "history": len(q.history(50)),
        })

    async def list_jobs(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "80") or "80")
        jobs = self.bot_app.queue.list_jobs()[-limit:]
        jobs = list(reversed(jobs))
        return _json({"ok": True, "jobs": [_job_to_dict(j) for j in jobs]})

    async def get_job(self, request: web.Request) -> web.Response:
        job = self.bot_app.queue.get(request.match_info["job_id"])
        if not job:
            return _error(404, "job tidak ditemukan")
        return _json({"ok": True, "job": _job_to_dict(job)})

    async def cancel_job(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        ok = await self.bot_app.queue.cancel(job_id)
        return _json({"ok": ok, "job_id": job_id})

    async def create_movie_job(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "body harus JSON")

        source = body.get("source") or {}
        tmdb = body.get("tmdb") or {}
        source_kind = str(source.get("kind") or "").strip()
        source_id = str(source.get("id") or source.get("file_id") or source.get("folder_id") or "").strip()
        source_name = str(source.get("name") or "").strip()
        target = str(body.get("target") or "player4me").lower().strip()

        if not source_id:
            return _error(400, "source.id kosong")
        if target != "player4me":
            return _error(400, "target sementara baru support player4me")

        title = str(tmdb.get("title") or body.get("title") or source_name or "Movie").strip()
        title = _human_title(title)

        if "folder" in source_kind:
            job_type = JobType.UPLOAD_PLAYER4ME_FOLDER
            url = _drive_folder_url(source_id)
        else:
            job_type = JobType.UPLOAD_PLAYER4ME_SUBS
            url = _drive_file_url(source_id)

        job = await self.bot_app.queue.enqueue(
            job_type=job_type,
            title=title,
            source_url=url,
            requested_by="drive-web",
        )
        return _json({"ok": True, "job": _job_to_dict(job)})

    async def create_series_jobs(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _error(400, "body harus JSON")

        tmdb = body.get("tmdb") or {}
        series_title = _human_title(str(tmdb.get("title") or body.get("title") or "Series"))
        season = int(body.get("season") or 1)
        target = str(body.get("target") or "player4me").lower().strip()
        episodes = body.get("episodes") or []

        if target != "player4me":
            return _error(400, "target sementara baru support player4me")
        if not isinstance(episodes, list) or not episodes:
            return _error(400, "episodes kosong")

        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for idx, ep in enumerate(episodes, start=1):
            if not isinstance(ep, dict):
                continue
            checked = ep.get("checked", True)
            if checked is False:
                skipped.append({"index": idx, "reason": "unchecked"})
                continue
            file_id = str(ep.get("drive_file_id") or ep.get("file_id") or ep.get("id") or "").strip()
            if not file_id:
                skipped.append({"index": idx, "reason": "drive_file_id kosong"})
                continue
            ep_no = int(ep.get("episode") or idx)
            ep_name = str(ep.get("name") or "").strip()
            title = f"{series_title} S{season:02d}E{ep_no:02d}"
            if ep_name:
                title += f" - {_human_title(ep_name)}"
            job = await self.bot_app.queue.enqueue(
                job_type=JobType.UPLOAD_PLAYER4ME_SUBS,
                title=title,
                source_url=_drive_file_url(file_id),
                requested_by="drive-web",
            )
            created.append(_job_to_dict(job))

        return _json({"ok": True, "created": created, "skipped": skipped})


async def start_bot_http_api(bot_app) -> BotHttpApi:
    api = BotHttpApi(bot_app)
    await api.start()
    bot_app.http_api = api
    return api
