import hashlib
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import APIRouter, File, Form, Response, UploadFile, status
from pydantic import HttpUrl, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiKey, DbSession, api_error
from app.config import get_settings
from app.models import Chunk, ChunkStatus, Job, JobStatus
from app.schemas import Transcription
from app.services import audio, storage
from app.services.export import MEDIA_TYPES, ExportFormat, render
from app.workers.tasks import process_job

router = APIRouter(prefix="/v1/transcriptions", tags=["transcriptions"])

_READ_CHUNK = 1024 * 1024
_HEADER_BYTES = 64
_url_adapter = TypeAdapter(HttpUrl)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=Transcription)
async def create_transcription(
    response: Response,
    session: DbSession,
    api_key: ApiKey,
    file: Annotated[UploadFile, File()],
    callback_url: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
) -> Transcription:
    """Accept an upload, store it, and queue transcription."""
    settings = get_settings()
    _validate_callback_url(callback_url)

    spooled = await _spool_upload(file, settings.max_upload_bytes)
    try:
        media_format = audio.detect_format(spooled.header)
        if media_format is None:
            raise api_error(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "File content does not match any supported audio or video container",
            )

        existing = await _find_completed_by_hash(session, spooled.digest)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return Transcription.from_job(existing, existing.total_chunks)

        job_id = uuid.uuid4()
        key = storage.raw_key(str(job_id), media_format)
        await anyio.to_thread.run_sync(
            storage.get_storage().upload_file, key, spooled.path, audio.CONTENT_TYPES[media_format]
        )
    finally:
        spooled.path.unlink(missing_ok=True)

    job = Job(
        id=job_id,
        status=JobStatus.QUEUED,
        filename=file.filename or f"upload.{media_format}",
        media_format=media_format,
        size_bytes=spooled.size,
        sha256=spooled.digest,
        storage_key=key,
        requested_language=language,
        callback_url=callback_url,
    )
    session.add(job)
    await session.commit()

    process_job.delay(str(job.id))
    return Transcription.from_job(job)


@router.get("/{job_id}", response_model=Transcription)
async def get_transcription(
    job_id: uuid.UUID, session: DbSession, api_key: ApiKey
) -> Transcription:
    """Return the current state of a job, including chunk progress."""
    job = await _get_job(session, job_id)
    return Transcription.from_job(job, await _completed_chunks(session, job_id))


@router.get("/{job_id}/export")
async def export_transcription(
    job_id: uuid.UUID, session: DbSession, api_key: ApiKey, format: ExportFormat = "json"
) -> Response:
    """Export a completed transcript as JSON, SRT or WebVTT."""
    job = await _get_job(session, job_id)
    if job.status is not JobStatus.COMPLETED:
        raise api_error(
            status.HTTP_409_CONFLICT,
            "not_completed",
            f"Job is {job.status.value}; export is available once it completes",
        )

    payload = Transcription.from_job(job).model_dump(mode="json")
    body = render(format, payload)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{job_id}.{format}"'},
    )


@router.post("/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=Transcription)
async def retry_transcription(
    job_id: uuid.UUID, session: DbSession, api_key: ApiKey
) -> Transcription:
    """Re-queue a failed job; chunks that already succeeded are reused."""
    job = await _get_job(session, job_id)
    if job.status is not JobStatus.FAILED:
        raise api_error(
            status.HTTP_409_CONFLICT,
            "not_retryable",
            f"Only failed jobs can be retried; job is {job.status.value}",
        )

    job.status = JobStatus.QUEUED
    job.error = None
    job.completed_at = None
    await session.commit()

    process_job.delay(str(job.id))
    return Transcription.from_job(job, await _completed_chunks(session, job_id))


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transcription(job_id: uuid.UUID, session: DbSession, api_key: ApiKey) -> Response:
    """Delete a job, its transcript and every stored object."""
    job = await _get_job(session, job_id)
    await anyio.to_thread.run_sync(
        storage.get_storage().delete_prefix, storage.job_prefix(str(job_id))
    )
    await session.delete(job)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@dataclass(frozen=True)
class _SpooledUpload:
    path: Path
    digest: str
    size: int
    header: bytes


async def _spool_upload(file: UploadFile, max_bytes: int) -> _SpooledUpload:
    """Stream the upload to disk, hashing as it arrives so nothing is held in memory."""
    digest = hashlib.sha256()
    size = 0
    header = b""

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
    path = Path(handle.name)
    try:
        while chunk := await file.read(_READ_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise api_error(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "file_too_large",
                    f"Upload exceeds the {max_bytes} byte limit",
                )
            if len(header) < _HEADER_BYTES:
                header += chunk[: _HEADER_BYTES - len(header)]
            digest.update(chunk)
            await anyio.to_thread.run_sync(handle.write, chunk)
    except BaseException:
        handle.close()
        path.unlink(missing_ok=True)
        raise
    finally:
        if not handle.closed:
            handle.close()

    if size == 0:
        path.unlink(missing_ok=True)
        raise api_error(status.HTTP_400_BAD_REQUEST, "empty_file", "Uploaded file is empty")

    return _SpooledUpload(path=path, digest=digest.hexdigest(), size=size, header=header)


def _validate_callback_url(callback_url: str | None) -> None:
    if callback_url is None:
        return
    try:
        _url_adapter.validate_python(callback_url)
    except ValidationError as exc:
        raise api_error(422, "invalid_callback_url", "callback_url is not a valid URL") from exc


async def _get_job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise api_error(status.HTTP_404_NOT_FOUND, "not_found", f"Job {job_id} does not exist")
    return job


async def _find_completed_by_hash(session: AsyncSession, digest: str) -> Job | None:
    statement = (
        select(Job)
        .where(Job.sha256 == digest, Job.status == JobStatus.COMPLETED)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return await session.scalar(statement)


async def _completed_chunks(session: AsyncSession, job_id: uuid.UUID) -> int:
    statement = (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.job_id == job_id, Chunk.status == ChunkStatus.COMPLETED)
    )
    return await session.scalar(statement) or 0
