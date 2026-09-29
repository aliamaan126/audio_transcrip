from functools import lru_cache

from app.config import get_settings
from app.engines.base import (
    Segment,
    TranscriptionEngine,
    TranscriptionError,
    TranscriptionResult,
)

__all__ = [
    "Segment",
    "TranscriptionEngine",
    "TranscriptionError",
    "TranscriptionResult",
    "get_engine",
]


@lru_cache(maxsize=1)
def get_engine() -> TranscriptionEngine:
    """Return the configured engine, loaded lazily so tests never import whisper."""
    name = get_settings().engine
    if name == "mock":
        from app.engines.mock_engine import MockEngine

        return MockEngine()

    from app.engines.whisper_engine import WhisperEngine

    return WhisperEngine()
