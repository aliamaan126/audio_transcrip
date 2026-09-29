import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.engines.base import Segment, TranscriptionError, TranscriptionResult


@lru_cache(maxsize=1)
def _load_model() -> Any:
    from faster_whisper import WhisperModel

    settings = get_settings()
    return WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )


def _confidence(avg_logprob: float | None) -> float:
    if avg_logprob is None:
        return 0.0
    return round(min(1.0, max(0.0, math.exp(avg_logprob))), 4)


class WhisperEngine:
    """faster-whisper backend with automatic language detection."""

    name = "whisper"

    def transcribe(self, audio_path: Path, language: str | None = None) -> TranscriptionResult:
        settings = get_settings()
        try:
            segments, info = _load_model().transcribe(
                str(audio_path),
                language=language,
                beam_size=settings.whisper_beam_size,
                vad_filter=False,
            )
            decoded = [
                Segment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                    confidence=_confidence(getattr(segment, "avg_logprob", None)),
                )
                for segment in segments
            ]
        except Exception as exc:  # engine internals vary; surface a uniform failure
            raise TranscriptionError(f"whisper transcription failed: {exc}") from exc

        return TranscriptionResult(
            language=info.language,
            duration=float(info.duration),
            segments=[segment for segment in decoded if segment.text],
        )
