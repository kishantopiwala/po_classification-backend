from typing import Any, Literal

from pydantic import BaseModel

JobStatus = Literal["queued", "processing", "completed", "failed"]


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
    events_url: str
    status_url: str


class JobError(BaseModel):
    code: str
    message: str


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    filename: str
    model: str
    document_id: str | None = None
    total_pages: int | None = None
    result: dict[str, Any] | None = None
    error: JobError | None = None
