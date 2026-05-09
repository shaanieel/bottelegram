"""Persistent async job queue.

Jobs are persisted to ``data/jobs.json`` so they survive restarts (status only —
in-flight downloads obviously cannot be resumed automatically). Workers pull
from the queue and dispatch to the downloader/uploader.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config_manager import AppConfig
from .logger import get_logger

log = get_logger(__name__)


# ----- Domain --------------------------------------------------------------- #

class JobType(str, Enum):
    UPLOAD_BUNNY = "upload_bunny"
    UPLOAD_PLAYER4ME = "upload_player4me"
    DOWNLOAD_ONLY = "download_only"


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    UPLOADING = "uploading"
    ENCODING = "encoding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    type: str
    title: str
    source_url: str
    status: str = JobStatus.QUEUED.value
    progress: float = 0.0
    progress_text: str = ""
    created_at: float = field(default_factory=lambda: time.time())
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_message: Optional[str] = None
    chat_id: Optional[int] = None
    user_id: Optional[int] = None
    requested_by: Optional[str] = None
    file_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    bunny_video_id: Optional[str] = None
    bunny_status: Optional[str] = None
    embed_url: Optional[str] = None
    player4me_task_id: Optional[str] = None
    player4me_video_id: Optional[str] = None
    player4me_status: Optional[str] = None
    player4me_engine: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        # Ignore unknown keys for forward compatibility
        valid = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**valid)


# Worker is a function that runs the job to completion. The QueueManager creates
# and passes a per-job ``cancel_event`` plus a ``progress`` coroutine.
WorkerFn = Callable[
    [Job, asyncio.Event, "ProgressFn"],
    Awaitable[None],
]
ProgressFn = Callable[[str, Optional[float], str], Awaitable[None]]


# ----- Queue manager -------------------------------------------------------- #

class QueueManager:
    """Single-process async queue with persistence and cancellation."""

    def __init__(
        self,
        cfg: AppConfig,
        worker: WorkerFn,
        on_status_change: Optional[Callable[[Job], Awaitable[None]]] = None,
    ) -> None:
        self.cfg = cfg
        self._worker = worker
        self._on_status_change = on_status_change

        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # insertion order
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._workers_running = False
        self._lock = asyncio.Lock()

        self._jobs_file: Path = cfg.paths.jobs_file
        self._jobs_file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---- public API ------------------------------------------------------ #

    def list_jobs(self) -> list[Job]:
        return [self._jobs[jid] for jid in self._order if jid in self._jobs]

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def queued_jobs(self) -> list[Job]:
        return [j for j in self.list_jobs() if j.status == JobStatus.QUEUED.value]

    def active_jobs(self) -> list[Job]:
        active = {
            JobStatus.DOWNLOADING.value,
            JobStatus.DOWNLOADED.value,
            JobStatus.UPLOADING.value,
            JobStatus.ENCODING.value,
        }
        return [j for j in self.list_jobs() if j.status in active]

    def history(self, limit: int = 10) -> list[Job]:
        finished = [j for j in self.list_jobs() if j.finished_at]
        finished.sort(key=lambda j: j.finished_at or 0, reverse=True)
        return finished[:limit]

    async def enqueue(
        self,
        *,
        job_type: JobType,
        title: str,
        source_url: str,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        requested_by: Optional[str] = None,
    ) -> Job:
        job_id = uuid.uuid4().hex[:8]
        job = Job(
            job_id=job_id,
            type=job_type.value,
            title=title or "Untitled",
            source_url=source_url,
            chat_id=chat_id,
            user_id=user_id,
            requested_by=requested_by,
        )
        async with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._cancel_events[job_id] = asyncio.Event()
            self._save()
        await self._queue.put(job_id)
        await self._notify(job)
        log.info("Enqueued job %s (%s) %s", job_id, job_type.value, title)
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status in (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        ):
            return False
        ev = self._cancel_events.get(job_id)
        if ev is not None:
            ev.set()
        if job.status == JobStatus.QUEUED.value:
            await self._update(
                job,
                status=JobStatus.CANCELLED,
                finished_at=time.time(),
                error_message="Dibatalkan sebelum dijalankan",
            )
        return True

    async def retry(self, job_id: str) -> Optional[Job]:
        old = self._jobs.get(job_id)
        if not old:
            return None
        if old.status not in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            return None
        return await self.enqueue(
            job_type=JobType(old.type),
            title=old.title,
            source_url=old.source_url,
            chat_id=old.chat_id,
            user_id=old.user_id,
            requested_by=old.requested_by,
        )

    async def start(self) -> None:
        if self._workers_running:
            return
        self._workers_running = True
        n = max(1, self.cfg.app.max_parallel_downloads)
        for i in range(n):
            asyncio.create_task(self._worker_loop(i), name=f"queue-worker-{i}")
        log.info("Queue started with %d worker(s)", n)

    async def stop(self) -> None:
        self._workers_running = False

    # ---- worker loop ----------------------------------------------------- #

    async def _worker_loop(self, idx: int) -> None:
        log.info("Worker #%d ready", idx)
        while self._workers_running:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if job.status == JobStatus.CANCELLED.value:
                continue

            cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
            await self._update(
                job,
                status=JobStatus.DOWNLOADING,
                started_at=time.time(),
                progress=0.0,
                progress_text="Mulai diproses",
                error_message=None,
            )

            async def progress(stage: str, pct: Optional[float], msg: str) -> None:
                await self._on_progress(job, stage, pct, msg)

            try:
                await self._worker(job, cancel_event, progress)
                if job.status not in (
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ):
                    await self._update(
                        job,
                        status=JobStatus.COMPLETED,
                        finished_at=time.time(),
                        progress=100.0,
                    )
            except asyncio.CancelledError:
                await self._update(
                    job,
                    status=JobStatus.CANCELLED,
                    finished_at=time.time(),
                    error_message="Dibatalkan",
                )
                raise
            except Exception as exc:
                log.exception("Job %s failed", job_id)
                await self._update(
                    job,
                    status=JobStatus.FAILED,
                    finished_at=time.time(),
                    error_message=str(exc),
                )

    # ---- helpers --------------------------------------------------------- #

    async def _on_progress(
        self,
        job: Job,
        stage: str,
        percent: Optional[float],
        message: str,
    ) -> None:
        status = _stage_to_status(stage, current=job.status)
        await self._update(
            job,
            status=status,
            progress=percent if percent is not None else job.progress,
            progress_text=message,
        )

    async def _update(self, job: Job, **fields) -> None:
        async with self._lock:
            for k, v in fields.items():
                if isinstance(v, JobStatus):
                    v = v.value
                setattr(job, k, v)
            self._save()
        await self._notify(job)

    async def _notify(self, job: Job) -> None:
        if self._on_status_change is None:
            return
        try:
            await self._on_status_change(job)
        except Exception:
            log.exception("on_status_change handler raised")

    def _save(self) -> None:
        try:
            self._jobs_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "jobs": [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs],
                "saved_at": time.time(),
            }
            tmp = self._jobs_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self._jobs_file)
        except OSError:
            log.exception("Failed to persist jobs.json")

    def _load(self) -> None:
        if not self._jobs_file.exists():
            return
        try:
            with self._jobs_file.open("r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            log.exception("Failed to load jobs.json (starting fresh)")
            return
        for raw in data.get("jobs", []):
            try:
                job = Job.from_dict(raw)
            except TypeError:
                continue
            # Anything that was in-flight at last shutdown is marked failed.
            in_flight = {
                JobStatus.DOWNLOADING.value,
                JobStatus.DOWNLOADED.value,
                JobStatus.UPLOADING.value,
                JobStatus.ENCODING.value,
                JobStatus.QUEUED.value,
            }
            if job.status in in_flight:
                job.status = JobStatus.FAILED.value
                job.error_message = "Bot restart sebelum job selesai"
                job.finished_at = time.time()
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
        # Prune to last N jobs to keep file small
        self._prune()

    def _prune(self) -> None:
        max_keep = max(50, self.cfg.app.history_size * 4)
        if len(self._order) <= max_keep:
            return
        drop = len(self._order) - max_keep
        for jid in self._order[:drop]:
            self._jobs.pop(jid, None)
            self._cancel_events.pop(jid, None)
        self._order = self._order[drop:]


def _stage_to_status(stage: str, current: str) -> JobStatus:
    mapping = {
        "starting": JobStatus.DOWNLOADING,
        "downloading": JobStatus.DOWNLOADING,
        "fallback": JobStatus.DOWNLOADING,
        "downloaded": JobStatus.DOWNLOADED,
        "creating": JobStatus.UPLOADING,
        "uploading": JobStatus.UPLOADING,
        "uploaded": JobStatus.ENCODING,
        "encoding": JobStatus.ENCODING,
    }
    return mapping.get(stage, JobStatus(current) if current else JobStatus.QUEUED)
