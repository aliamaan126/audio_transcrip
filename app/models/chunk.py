import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JsonB, TimestampMixin
from app.models.job import Job


class ChunkStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Chunk(Base, TimestampMixin):
    """One silence-aligned slice of a long job, transcribed independently."""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("job_id", "index", name="uq_chunks_job_index"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int]
    offset_seconds: Mapped[float]
    duration_seconds: Mapped[float]
    storage_key: Mapped[str] = mapped_column(String(512))

    status: Mapped[ChunkStatus] = mapped_column(
        SAEnum(
            ChunkStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ChunkStatus.PENDING,
        index=True,
    )
    language: Mapped[str | None] = mapped_column(String(16))
    segments: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonB)
    error: Mapped[str | None]

    job: Mapped[Job] = relationship(back_populates="chunks")
