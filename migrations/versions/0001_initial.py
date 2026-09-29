"""initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_status = sa.Enum(
    "queued", "processing", "completed", "failed", name="job_status", native_enum=False, length=16
)
chunk_status = sa.Enum(
    "pending", "completed", "failed", name="chunk_status", native_enum=False, length=16
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", job_status, nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("media_format", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("requested_language", sa.String(16), nullable=True),
        sa.Column("callback_url", sa.String(2048), nullable=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("segments", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_sha256", "jobs", ["sha256"])
    op.create_index("ix_jobs_sha256_status", "jobs", ["sha256", "status"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("offset_seconds", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("status", chunk_status, nullable=False),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("segments", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("job_id", "index", name="uq_chunks_job_index"),
    )
    op.create_index("ix_chunks_job_id", "chunks", ["job_id"])
    op.create_index("ix_chunks_status", "chunks", ["status"])


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("jobs")
