import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Job, JobStatus


class Segment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    confidence: float


class Progress(BaseModel):
    completed_chunks: int
    total_chunks: int


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Transcription(BaseModel):
    """Public representation of a transcription job."""

    job_id: uuid.UUID
    status: JobStatus
    language: str | None = None
    duration: float | None = None
    full_text: str | None = None
    segments: list[Segment] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    progress: Progress | None = None

    @classmethod
    def from_job(cls, job: Job, completed_chunks: int = 0) -> "Transcription":
        progress = (
            Progress(completed_chunks=completed_chunks, total_chunks=job.total_chunks)
            if job.total_chunks
            else None
        )
        return cls(
            job_id=job.id,
            status=job.status,
            language=job.language,
            duration=job.duration,
            full_text=job.full_text,
            segments=[Segment(**segment) for segment in job.segments or []],
            error=job.error,
            created_at=job.created_at,
            completed_at=job.completed_at,
            progress=progress,
        )
