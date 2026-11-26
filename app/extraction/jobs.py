"""Single-process bounded job runner, independent of SSE subscribers."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .events import Event
from .schemas import JobResponse
from .service import ExtractionError, extract

logger = logging.getLogger(__name__)


class CapacityError(Exception):
    pass


@dataclass
class Job:
    id: str
    path: Path
    filename: str
    model: str
    document_id: str | None = None
    status: str = "queued"
    total_pages: int | None = None
    result: dict | None = None
    error: dict | None = None
    finished_at: float | None = None
    events: list[Event] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def snapshot(self):
        return JobResponse(
            job_id=self.id,
            status=self.status,
            filename=self.filename,
            model=self.model,
            document_id=self.document_id,
            total_pages=self.total_pages,
            result=self.result,
            error=self.error,
        )

    def emit(self, name, **details):
        self.events.append(
            Event(
                len(self.events) + 1,
                name,
                {"job_id": self.id, "status": self.status, **details},
            )
        )
        self.changed.set()


class JobManager:
    def __init__(self, settings, extractor=extract, po_service=None):
        self.settings = settings
        self.extractor = extractor
        self.po_service = po_service
        self.jobs: dict[str, Job] = {}
        self.queue = asyncio.Queue(maxsize=settings.queue_size)
        self.accepting = True
        self.reserved = 0

    async def start(self):
        self.worker = asyncio.create_task(self._work())
        self.reaper = asyncio.create_task(self._reap())

    def reserve(self):
        self.prune()
        if (
            not self.accepting
            or self.queue.qsize() + self.reserved >= self.settings.queue_size
            or len(self.jobs) + self.reserved >= self.settings.max_jobs
        ):
            raise CapacityError("Job capacity reached. Retry later.")
        self.reserved += 1

    def submit(self, path, filename):
        job = Job(str(uuid4()), path, filename, self.settings.model)
        self.queue.put_nowait(job)
        self.jobs[job.id] = job
        job.emit("queued")
        return job

    def prune(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if (
                job.finished_at is not None
                and now - job.finished_at >= self.settings.retention
            ):
                del self.jobs[key]

    def get(self, key):
        self.prune()
        return self.jobs.get(key)

    async def _reap(self):
        while True:
            await asyncio.sleep(min(self.settings.retention, 60))
            self.prune()

    async def _work(self):
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            job.status = "processing"
            job.emit("progress", stage="starting")

            def progress(details, current_job=job):
                # Worker threads never mutate asyncio-owned state directly.
                loop.call_soon_threadsafe(self._progress, current_job, details)

            try:
                args = SimpleNamespace(
                    pdf=job.path,
                    host=self.settings.host,
                    model=self.settings.model,
                    dpi=self.settings.dpi,
                    timeout=self.settings.timeout,
                    num_ctx=self.settings.num_ctx,
                    num_predict=self.settings.num_predict,
                )
                output = await asyncio.to_thread(
                    self.extractor,
                    args,
                    progress=progress,
                    max_pages=self.settings.max_pages,
                    document_name=job.filename,
                )
                job.total_pages = output["total_pages"]
                if self.po_service:
                    job.document_id = await self.po_service.ingest(
                        job.path, job.filename, output
                    )
                job.result = output["result"]
                job.status = "completed"
                job.emit(
                    "completed",
                    **job.snapshot().model_dump(exclude={"job_id", "status"}),
                )
            except Exception as exc:
                logger.exception("Extraction failed for job %s", job.id)
                job.status = "failed"
                # Full diagnostic details stay in server logs (paths, upstream responses).
                message = "PDF extraction failed. Check the PDF and Ollama server."
                if isinstance(exc, ExtractionError) and "-page limit" in str(exc):
                    message = str(exc)
                job.error = {"code": "extraction_failed", "message": message}
                job.emit("failed", error=job.error)
            finally:
                job.path.unlink(missing_ok=True)
                job.finished_at = time.monotonic()
                self.queue.task_done()

    @staticmethod
    def _progress(job, details):
        if job.status == "processing":
            if "total_pages" in details:
                job.total_pages = details["total_pages"]
            job.emit("progress", **details)

    async def close(self):
        self.accepting = False
        self.reaper.cancel()
        await asyncio.gather(self.reaper, return_exceptions=True)
        # Discard queued jobs; let the active thread finish before removing its files.
        while not self.queue.empty():
            job = self.queue.get_nowait()
            job.path.unlink(missing_ok=True)
            self.queue.task_done()
        await self.queue.put(None)
        await self.worker
