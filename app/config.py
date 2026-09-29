from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_ASYNC_DRIVERS = {"sqlite": "sqlite+aiosqlite", "postgresql": "postgresql+psycopg"}


class Settings(BaseSettings):
    """Runtime configuration, entirely sourced from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["local-dev-key"])
    max_upload_bytes: int = 512 * 1024 * 1024

    database_url: str = "postgresql+psycopg://postgres:postgres@postgres:5432/transcriptions"
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "audio"
    s3_region: str = "us-east-1"

    engine: Literal["whisper", "mock"] = "whisper"
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_beam_size: int = 5

    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    ffmpeg_timeout_seconds: int = 1800

    chunk_threshold_seconds: float = 600.0
    chunk_min_seconds: float = 30.0
    chunk_target_seconds: float = 45.0
    chunk_max_seconds: float = 60.0
    chunk_overlap_seconds: float = 1.0
    silence_noise_db: int = -30
    silence_min_duration: float = 0.35

    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    raw_audio_retention_days: int = 30
    retention_cleanup_hours: int = 6

    webhook_timeout_seconds: float = 10.0
    task_max_retries: int = 3
    task_retry_backoff_seconds: int = 10

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value

    @property
    def async_database_url(self) -> str:
        """Same database, using the asyncio-capable driver for the API process."""
        scheme, _, rest = self.database_url.partition("://")
        dialect = scheme.split("+", 1)[0]
        return f"{_ASYNC_DRIVERS.get(dialect, scheme)}://{rest}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
