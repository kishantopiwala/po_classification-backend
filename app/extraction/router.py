import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .events import stream_events
from .jobs import CapacityError
from .schemas import JobAccepted, JobResponse

router = APIRouter(prefix="/extractions", tags=["extractions"])


def get_job(request, job_id):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired")
    return job


@router.post("", status_code=202, response_model=JobAccepted)
async def upload(request: Request, file: Annotated[UploadFile, File()]):
    manager = request.app.state.jobs
    settings = manager.settings
    path = None
    submitted = False
    reserved = False
    try:
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename.lower().endswith(".pdf") or len(filename) > 255:
            raise HTTPException(415, "Upload a PDF file with a .pdf filename")
        if file.content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(415, "Unsupported file content type")
        try:
            manager.reserve()
            reserved = True
        except CapacityError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "15"}) from exc
        descriptor, name = tempfile.mkstemp(
            prefix="po-upload-", suffix=".pdf", dir=settings.upload_dir
        )
        os.close(descriptor)
        path = Path(name)
        size = 0
        with path.open("wb") as output:
            while chunk := await file.read(64 * 1024):
                if size == 0 and b"%PDF-" not in chunk[:1024]:
                    raise HTTPException(400, "File does not contain a PDF header")
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "PDF exceeds the upload size limit")
                await run_in_threadpool(output.write, chunk)
        if size == 0:
            raise HTTPException(400, "PDF is empty")
        job = manager.submit(path, filename)
        submitted = True
        base = f"/api/v1/extractions/{job.id}"
        return JobAccepted(
            job_id=job.id, status="queued", events_url=f"{base}/events", status_url=base
        )
    finally:
        if reserved:
            manager.reserved -= 1
        if path is not None and not submitted:
            path.unlink(missing_ok=True)
        await file.close()


@router.get("/{job_id}", response_model=JobResponse)
async def status(request: Request, job_id: str):
    return get_job(request, job_id).snapshot()


@router.get("/{job_id}/events")
async def events(
    request: Request, job_id: str, last_event_id: Annotated[str | None, Header()] = None
):
    job = get_job(request, job_id)
    try:
        after = int(last_event_id) if last_event_id is not None else 0
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID must be a nonnegative integer") from exc
    if after < 0 or after > len(job.events):
        raise HTTPException(400, "Last-Event-ID is outside this job's event history")
    return StreamingResponse(
        stream_events(job, after, request.app.state.jobs.settings.heartbeat),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
