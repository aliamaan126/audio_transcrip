import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from celery import chord
from celery.utils.log import get_task_logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.engines import get_engine
from app.models import Chunk, ChunkStatus, Job, JobStatus
from app.schemas import Transcription
from app.services import audio, storage
from app.services.merge import merge_chunk_segments, segments_to_text, to_dicts
from app.workers.celery_app import celery_app

logger = get_task_logger(__name__)
settings = get_settings()

AUTORETRY = {
    "autoretry_for": (Exception,),
    "retry_backoff": settings.task_retry_backoff_seconds,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": settings.task_max_retries,
}


@celery_app.task(bind=True, name="jobs.process", **AUTORETRY)
def process_job(self, job_id: str) -> str:
    """Normalize the raw upload and either transcribe it directly or fan out into chunks."""
    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.warning("job %s no longer exists", job_id)
            return job_id
        if job.status is JobStatus.COMPLETED:
            return job_id

        storage_key = job.storage_key
        media_format = job.media_format
        language = job.requested_language
        if storage_key is not None:
            job.status = JobStatus.PROCESSING
            job.error = None

    if storage_key is None:
        _fail(job_id, "raw audio is no longer available")
        return job_id

    try:
        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-") as workdir:
            root = Path(workdir)
            raw_path = root / f"raw.{media_format}"
            storage.get_storage().download_file(storage_key, raw_path)

            source = audio.probe(raw_path)
            logger.info(
                "job %s source: %s %dHz %dch %.1fs",
                job_id,
                source.codec,
                source.sample_rate,
                source.channels,
                source.duration,
            )
            normalized = audio.normalize(raw_path, root / "normalized.wav")
            info = audio.probe(normalized)

            if info.duration <= settings.chunk_threshold_seconds:
                _transcribe_whole(job_id, normalized, info.duration, language)
            else:
                _dispatch_chunks(job_id, normalized, info.duration, root)
    except audio.AudioError as exc:
        _fail(job_id, str(exc))
        return job_id
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail(job_id, f"processing failed after {self.max_retries} retries: {exc}")
        raise

    return job_id


@celery_app.task(bind=True, name="jobs.transcribe_chunk")
def transcribe_chunk(self, job_id: str, chunk_id: str) -> str:
    """Transcribe one chunk. Retries are manual so the chord always runs its callback."""
    with session_scope() as session:
        chunk = session.get(Chunk, uuid.UUID(chunk_id))
        if chunk is None or chunk.status is ChunkStatus.COMPLETED:
            return chunk_id
        storage_key = chunk.storage_key
        language = chunk.job.requested_language

    try:
        with tempfile.TemporaryDirectory(prefix=f"chunk-{chunk_id}-") as workdir:
            local = Path(workdir) / "chunk.wav"
            storage.get_storage().download_file(storage_key, local)
            result = get_engine().transcribe(local, language)

        with session_scope() as session:
            chunk = session.get(Chunk, uuid.UUID(chunk_id))
            chunk.segments = to_dicts(result.segments)
            chunk.language = result.language
            chunk.status = ChunkStatus.COMPLETED
            chunk.error = None
    except Exception as exc:
        if self.request.retries < settings.task_max_retries:
            raise self.retry(
                exc=exc, countdown=settings.task_retry_backoff_seconds * 2**self.request.retries
            ) from exc
        logger.exception("chunk %s exhausted retries", chunk_id)
        with session_scope() as session:
            chunk = session.get(Chunk, uuid.UUID(chunk_id))
            chunk.status = ChunkStatus.FAILED
            chunk.error = str(exc)

    return chunk_id


@celery_app.task(name="jobs.finalize")
def finalize_job(chunk_ids: list[str], job_id: str) -> str:
    """Merge chunk transcripts once the whole group has settled."""
    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            return job_id

        chunks = session.scalars(
            select(Chunk).where(Chunk.job_id == job.id).order_by(Chunk.index)
        ).all()
        failed = [chunk for chunk in chunks if chunk.status is not ChunkStatus.COMPLETED]
        if failed:
            _fail_job(
                session, job, f"{len(failed)} of {len(chunks)} chunks failed: {failed[0].error}"
            )
        else:
            segments = merge_chunk_segments(
                (chunk.offset_seconds, chunk.segments or []) for chunk in chunks
            )
            job.segments = segments
            job.full_text = segments_to_text(segments)
            job.language = next((chunk.language for chunk in chunks if chunk.language), None)
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(UTC)
            job.error = None

    if not failed:
        storage.get_storage().delete_prefix(storage.chunk_prefix(job_id))

    deliver_webhook.delay(job_id)
    return job_id


@celery_app.task(bind=True, name="jobs.webhook", **AUTORETRY)
def deliver_webhook(self, job_id: str) -> None:
    """POST the final job state to the caller-supplied callback URL."""
    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None or not job.callback_url:
            return
        url = job.callback_url
        payload = Transcription.from_job(job, _completed_chunks(session, job.id)).model_dump(
            mode="json"
        )

    response = httpx.post(url, json=payload, timeout=settings.webhook_timeout_seconds)
    response.raise_for_status()


@celery_app.task(name="jobs.cleanup_expired_audio")
def cleanup_expired_audio() -> int:
    """Drop raw uploads past the retention window; transcripts are kept."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.raw_audio_retention_days)
    removed = 0
    with session_scope() as session:
        jobs = session.scalars(
            select(Job).where(Job.storage_key.is_not(None), Job.created_at < cutoff)
        ).all()
        for job in jobs:
            storage.get_storage().delete(job.storage_key)
            job.storage_key = None
            removed += 1
    return removed


def _transcribe_whole(job_id: str, path: Path, duration: float, language: str | None) -> None:
    result = get_engine().transcribe(path, language)
    segments = to_dicts(result.segments)
    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.language = result.language
        job.duration = duration
        job.segments = segments
        job.full_text = segments_to_text(segments)
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)
        job.error = None
    deliver_webhook.delay(job_id)


def _dispatch_chunks(job_id: str, normalized: Path, duration: float, workdir: Path) -> None:
    plans = audio.plan_chunks(duration, audio.detect_silences(normalized))
    object_storage = storage.get_storage()

    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        job.duration = duration
        job.total_chunks = len(plans)
        existing = {
            chunk.index: chunk
            for chunk in session.scalars(select(Chunk).where(Chunk.job_id == job.id))
        }

        pending: list[tuple[str, audio.ChunkPlan]] = []
        for plan in plans:
            chunk = existing.get(plan.index)
            if chunk is not None and chunk.status is ChunkStatus.COMPLETED:
                continue
            if chunk is None:
                chunk = Chunk(
                    job_id=job.id,
                    index=plan.index,
                    offset_seconds=plan.start,
                    duration_seconds=plan.duration,
                    storage_key=storage.chunk_key(job_id, plan.index),
                )
                session.add(chunk)
                session.flush()
            chunk.status = ChunkStatus.PENDING
            chunk.error = None
            pending.append((str(chunk.id), plan))

    for _, plan in pending:
        local = workdir / f"chunk-{plan.index:05d}.wav"
        audio.extract_chunk(normalized, local, plan.start, plan.duration)
        object_storage.upload_file(storage.chunk_key(job_id, plan.index), local, "audio/wav")
        local.unlink(missing_ok=True)

    if not pending:
        finalize_job.delay([], job_id)
        return

    chord(transcribe_chunk.s(job_id, chunk_id) for chunk_id, _ in pending)(finalize_job.s(job_id))


def _completed_chunks(session: Session, job_id: uuid.UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.job_id == job_id, Chunk.status == ChunkStatus.COMPLETED)
        )
        or 0
    )


def _fail(job_id: str, message: str) -> None:
    with session_scope() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is not None:
            _fail_job(session, job, message)
    deliver_webhook.delay(job_id)


def _fail_job(session: Session, job: Job, message: str) -> None:
    job.status = JobStatus.FAILED
    job.error = message
    job.completed_at = datetime.now(UTC)
    session.flush()
