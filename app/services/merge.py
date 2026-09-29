import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.engines.base import Segment

_WORD_NOISE = re.compile(r"[^\w']+", re.UNICODE)
_EPSILON = 0.01
_TAIL_SEGMENTS = 3


def to_dicts(segments: Sequence[Segment]) -> list[dict[str, Any]]:
    """Convert engine segments into the serialisable transcript shape."""
    return [
        {
            "id": index,
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
            "confidence": segment.confidence,
        }
        for index, segment in enumerate(segments)
    ]


def merge_chunk_segments(
    chunks: Iterable[tuple[float, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    """Offset chunk-local timestamps, drop words duplicated in overlaps, and merge in order."""
    merged: list[dict[str, Any]] = []

    for offset, segments in sorted(chunks, key=lambda item: item[0]):
        for segment in segments:
            shifted = {
                "id": 0,
                "start": round(float(segment["start"]) + offset, 3),
                "end": round(float(segment["end"]) + offset, 3),
                "text": str(segment.get("text", "")).strip(),
                "confidence": float(segment.get("confidence", 0.0)),
            }
            if not shifted["text"]:
                continue

            deduped = _deduplicate(merged, shifted)
            if deduped is not None:
                merged.append(deduped)

    for index, segment in enumerate(merged):
        segment["id"] = index
    return merged


def segments_to_text(segments: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(str(segment["text"]).strip() for segment in segments if segment["text"])


def _deduplicate(merged: list[dict[str, Any]], segment: dict[str, Any]) -> dict[str, Any] | None:
    """Trim the leading words of a segment that repeat audio already transcribed."""
    if not merged or segment["start"] >= merged[-1]["end"] - _EPSILON:
        return segment

    tail = [s for s in merged[-_TAIL_SEGMENTS:] if s["end"] > segment["start"] - _EPSILON]
    if not tail:
        return segment

    tail_words = [word for s in tail for word in s["text"].split()]
    head_words = segment["text"].split()
    repeated = _common_run(tail_words, head_words)
    if repeated == 0:
        return segment
    if repeated == len(head_words):
        return None

    span = segment["end"] - segment["start"]
    segment["start"] = round(segment["start"] + span * repeated / len(head_words), 3)
    segment["text"] = " ".join(head_words[repeated:])
    return segment


def _common_run(tail: list[str], head: list[str]) -> int:
    normalized_tail = [_normalize(word) for word in tail]
    normalized_head = [_normalize(word) for word in head]
    for size in range(min(len(normalized_tail), len(normalized_head)), 0, -1):
        if normalized_tail[-size:] == normalized_head[:size]:
            return size
    return 0


def _normalize(word: str) -> str:
    return _WORD_NOISE.sub("", word.casefold())
