import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

ExportFormat = Literal["json", "srt", "vtt"]

MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


def render(fmt: ExportFormat, payload: Mapping[str, Any]) -> str:
    """Serialise a transcript payload into the requested subtitle or data format."""
    segments = payload.get("segments") or []
    if fmt == "json":
        return json.dumps(payload, indent=2, default=str)
    if fmt == "srt":
        return to_srt(segments)
    return to_vtt(segments)


def to_srt(segments: Sequence[Mapping[str, Any]]) -> str:
    blocks = [f"{index}\n{_cue(s, ',')}" for index, s in enumerate(segments, start=1)]
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def to_vtt(segments: Sequence[Mapping[str, Any]]) -> str:
    blocks = [_cue(s, ".") for s in segments]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def _cue(segment: Mapping[str, Any], separator: str) -> str:
    start = _timestamp(segment["start"], separator)
    end = _timestamp(segment["end"], separator)
    return f"{start} --> {end}\n{segment['text'].strip()}"


def _timestamp(seconds: float, millisecond_separator: str) -> str:
    total_ms = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{millisecond_separator}{millis:03d}"
