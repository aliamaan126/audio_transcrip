import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JsonB, TimestampMixin, TimestampTZ


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(Base, TimestampMixin):
    """A single transcription request and its result."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_sha256_status", "sha256", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(
            JobStatus, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]
        ),
        default=JobStatus.QUEUED,
        index=True,
    )

    filename: Mapped[str] = mapped_column(String(512))
    media_format: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(512))

    requested_language: Mapped[str | None] = mapped_column(String(16))
    callback_url: Mapped[str | None] = mapped_column(String(2048))

    language: Mapped[str | None] = mapped_column(String(16))
    duration: Mapped[float | None]
    full_text: Mapped[str | None]
    segments: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonB)
    error: Mapped[str | None]

    total_chunks: Mapped[int] = mapped_column(default=0)
    completed_at: Mapped[datetime | None] = mapped_column(TimestampTZ)

    chunks: Mapped[list["Chunk"]] = relationship(  # noqa: F821
        back_populates="job", cascade="all, delete-orphan", order_by="Chunk.index"
    )
