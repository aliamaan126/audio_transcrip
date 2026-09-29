import os
import shutil
import tempfile
import wave
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="transcription-tests-"))

os.environ.update(
    {
        "DATABASE_URL": f"sqlite+pysqlite:///{(_TMP_ROOT / 'test.db').as_posix()}",
        "REDIS_URL": "redis://localhost:6379/15",
        "ENGINE": "mock",
        "API_KEYS": "test-key",
        "RATE_LIMIT_REQUESTS": "0",
        "S3_BUCKET": "test-audio",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import audio, storage  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_audio"
SAMPLE_WAV = SAMPLE_DIR / "speech_16k_mono.wav"
SAMPLE_MP3 = SAMPLE_DIR / "speech_mono.mp3"


class LocalStorage:
    """Filesystem stand-in for MinIO with the same surface as ObjectStorage."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_bucket(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def upload_fileobj(self, key: str, fileobj, content_type: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            shutil.copyfileobj(fileobj, handle)

    def upload_file(self, key: str, path: Path, content_type: str | None = None) -> None:
        with Path(path).open("rb") as handle:
            self.upload_fileobj(key, handle)

    def download_file(self, key: str, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), path)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._path(prefix), ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    engine = create_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    engine.dispose()
    yield


@pytest.fixture(autouse=True)
def local_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorage:
    backend = LocalStorage(tmp_path / "objects")
    backend.ensure_bucket()
    monkeypatch.setattr(storage, "get_storage", lambda: backend)
    return backend


@pytest.fixture(autouse=True)
def eager_celery() -> Iterator[None]:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture
def stub_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the ffmpeg layer with a WAV-only equivalent so API tests run anywhere."""
    monkeypatch.setattr(audio, "probe", _wave_probe)
    monkeypatch.setattr(audio, "normalize", _copy_wav)
    monkeypatch.setattr(audio, "extract_chunk", _slice_wav)
    monkeypatch.setattr(audio, "detect_silences", lambda path: [])


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


def _wave_probe(path: Path) -> audio.AudioInfo:
    with wave.open(str(path), "rb") as handle:
        return audio.AudioInfo(
            codec="pcm_s16le",
            sample_rate=handle.getframerate(),
            channels=handle.getnchannels(),
            duration=handle.getnframes() / float(handle.getframerate()),
        )


def _copy_wav(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def _slice_wav(source: Path, destination: Path, start: float, duration: float) -> Path:
    with wave.open(str(source), "rb") as reader:
        rate = reader.getframerate()
        reader.setpos(min(int(start * rate), reader.getnframes()))
        frames = reader.readframes(int(duration * rate))
        params = reader.getparams()

    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(params.sampwidth)
        writer.setframerate(params.framerate)
        writer.writeframes(frames)
    return destination
