import hashlib
import random
import wave
from pathlib import Path

from app.engines.base import Segment, TranscriptionError, TranscriptionResult

_LEXICON = (
    "the quick brown fox jumps over a lazy dog while the engineer reviews "
    "distributed queues storage buckets and transcription latency budgets"
).split()

_SEGMENT_SECONDS = 5.0


def _wav_duration(audio_path: Path) -> float:
    try:
        with wave.open(str(audio_path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (OSError, wave.Error) as exc:
        raise TranscriptionError(f"mock engine expects 16-bit PCM WAV input: {exc}") from exc


class MockEngine:
    """Deterministic backend for tests and offline development."""

    name = "mock"

    def transcribe(self, audio_path: Path, language: str | None = None) -> TranscriptionResult:
        duration = _wav_duration(audio_path)
        digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        rng = random.Random(digest)

        segments: list[Segment] = []
        start = 0.0
        while start < duration:
            end = min(start + _SEGMENT_SECONDS, duration)
            words = rng.sample(_LEXICON, k=rng.randint(4, 8))
            segments.append(
                Segment(
                    start=round(start, 3),
                    end=round(end, 3),
                    text=" ".join(words),
                    confidence=round(rng.uniform(0.75, 0.99), 4),
                )
            )
            start = end

        return TranscriptionResult(
            language=language or "en", duration=round(duration, 3), segments=segments
        )
