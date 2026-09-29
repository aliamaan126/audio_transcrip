from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    confidence: float


@dataclass(frozen=True)
class TranscriptionResult:
    language: str | None
    duration: float
    segments: list[Segment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments if segment.text.strip())


class TranscriptionError(RuntimeError):
    """Raised when an engine cannot transcribe the given audio."""


@runtime_checkable
class TranscriptionEngine(Protocol):
    """Contract every speech-to-text backend implements."""

    name: str

    def transcribe(self, audio_path: Path, language: str | None = None) -> TranscriptionResult: ...
