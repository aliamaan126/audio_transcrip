import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.engines.base import TranscriptionError, TranscriptionResult
from app.engines.mock_engine import MockEngine
from app.models import Chunk, ChunkStatus, Job, JobStatus
from app.services import storage
from app.workers import tasks
from tests.conftest import SAMPLE_WAV

pytestmark = pytest.mark.usefixtures("stub_ffmpeg")


class FailingEngine:
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio_path: Path, language: str | None = None) -> TranscriptionResult:
        self.calls += 1
        raise TranscriptionError("engine exploded")


class CountingEngine:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0
        self._inner = MockEngine()

    def transcribe(self, audio_path: Path, language: str | None = None) -> TranscriptionResult:
        self.calls += 1
        return self._inner.transcribe(audio_path, language)


@pytest.fixture
def short_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "chunk_threshold_seconds", 2.0)
    monkeypatch.setattr(settings, "chunk_min_seconds", 1.0)
    monkeypatch.setattr(settings, "chunk_target_seconds", 2.0)
    monkeypatch.setattr(settings, "chunk_max_seconds", 2.0)
    monkeypatch.setattr(settings, "chunk_overlap_seconds", 0.5)


async def upload(client: AsyncClient, headers: dict[str, str], **fields: str) -> str:
    response = await client.post(
        "/v1/transcriptions",
        files={"file": ("speech.wav", SAMPLE_WAV.read_bytes(), "application/octet-stream")},
        data=fields,
        headers=headers,
    )
    return response.json()["job_id"]


def load_job(job_id: str) -> Job:
    with session_scope() as session:
        return session.get(Job, uuid.UUID(job_id))


def load_chunks(job_id: str) -> list[Chunk]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(Chunk).where(Chunk.job_id == uuid.UUID(job_id)).order_by(Chunk.index)
            )
        )


async def test_job_fails_after_the_engine_exhausts_retries(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FailingEngine()
    monkeypatch.setattr(tasks, "get_engine", lambda: engine)

    job_id = await upload(client, auth_headers)
    job = load_job(job_id)

    assert job.status is JobStatus.FAILED
    assert "engine exploded" in job.error
    assert engine.calls == get_settings().task_max_retries + 1


async def test_retry_endpoint_reprocesses_a_failed_job(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "get_engine", lambda: FailingEngine())
    job_id = await upload(client, auth_headers)
    assert load_job(job_id).status is JobStatus.FAILED

    monkeypatch.setattr(tasks, "get_engine", MockEngine)
    response = await client.post(f"/v1/transcriptions/{job_id}/retry", headers=auth_headers)

    assert response.status_code == 202
    job = load_job(job_id)
    assert job.status is JobStatus.COMPLETED
    assert job.full_text
    assert job.error is None


async def test_long_audio_is_split_merged_and_cleaned_up(
    client: AsyncClient, auth_headers: dict[str, str], short_chunks: None, local_storage
) -> None:
    job_id = await upload(client, auth_headers)
    job = load_job(job_id)
    chunks = load_chunks(job_id)

    assert job.status is JobStatus.COMPLETED
    assert job.total_chunks == len(chunks) > 1
    assert all(chunk.status is ChunkStatus.COMPLETED for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    starts = [segment["start"] for segment in job.segments]
    assert starts == sorted(starts)
    assert job.segments[-1]["end"] <= job.duration + 0.5
    assert not (local_storage.root / storage.chunk_prefix(job_id)).exists()


async def test_progress_is_reported_for_chunked_jobs(
    client: AsyncClient, auth_headers: dict[str, str], short_chunks: None
) -> None:
    job_id = await upload(client, auth_headers)

    body = (await client.get(f"/v1/transcriptions/{job_id}", headers=auth_headers)).json()

    assert body["progress"]["total_chunks"] == body["progress"]["completed_chunks"] > 1


async def test_retry_only_reruns_chunks_that_are_not_complete(
    client: AsyncClient,
    auth_headers: dict[str, str],
    short_chunks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "get_engine", lambda: FailingEngine())
    job_id = await upload(client, auth_headers)
    assert load_job(job_id).status is JobStatus.FAILED

    with session_scope() as session:
        first = session.scalars(
            select(Chunk).where(Chunk.job_id == uuid.UUID(job_id), Chunk.index == 0)
        ).one()
        first.status = ChunkStatus.COMPLETED
        first.segments = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "already transcribed", "confidence": 0.9}
        ]

    engine = CountingEngine()
    monkeypatch.setattr(tasks, "get_engine", lambda: engine)
    await client.post(f"/v1/transcriptions/{job_id}/retry", headers=auth_headers)

    job = load_job(job_id)
    assert job.status is JobStatus.COMPLETED
    assert engine.calls == job.total_chunks - 1
    assert job.full_text.startswith("already transcribed")


async def test_webhook_is_delivered_with_the_final_payload(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[tuple[str, dict]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, json: dict, timeout: float) -> _Response:
        delivered.append((url, json))
        return _Response()

    monkeypatch.setattr(tasks.httpx, "post", fake_post)

    job_id = await upload(client, auth_headers, callback_url="https://example.test/hook")

    assert delivered
    url, payload = delivered[0]
    assert url == "https://example.test/hook"
    assert payload["job_id"] == job_id
    assert payload["status"] == "completed"


def test_cleanup_removes_raw_audio_past_retention(local_storage) -> None:
    settings = get_settings()
    job_id = uuid.uuid4()
    key = storage.raw_key(str(job_id), "wav")
    local_storage.upload_file(key, SAMPLE_WAV)

    with session_scope() as session:
        session.add(
            Job(
                id=job_id,
                status=JobStatus.COMPLETED,
                filename="speech.wav",
                media_format="wav",
                size_bytes=SAMPLE_WAV.stat().st_size,
                sha256="a" * 64,
                storage_key=key,
                created_at=datetime.now(UTC)
                - timedelta(days=settings.raw_audio_retention_days + 1),
                updated_at=datetime.now(UTC),
            )
        )

    assert tasks.cleanup_expired_audio() == 1
    assert load_job(str(job_id)).storage_key is None
    assert not (local_storage.root / key).exists()
