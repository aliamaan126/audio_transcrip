import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1

_HEADER_BYTES = 64
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")

_ISOBMFF_BRANDS = {
    b"M4A ": "m4a",
    b"M4B ": "m4a",
    b"mp42": "mp4",
    b"mp41": "mp4",
    b"isom": "mp4",
    b"iso2": "mp4",
    b"avc1": "mp4",
    b"qt  ": "mov",
}

CONTENT_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
}


class AudioError(RuntimeError):
    """Raised for unsupported, corrupt or unprocessable media."""


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    sample_rate: int
    channels: int
    duration: float


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start: float
    duration: float


def detect_format(header: bytes) -> str | None:
    """Identify the container from magic bytes; returns None when unsupported."""
    if len(header) < 12:
        return None
    if header[:4] == b"RIFF":
        if header[8:12] == b"WAVE":
            return "wav"
        if header[8:12] == b"AVI ":
            return "avi"
        return None
    if header[:3] == b"ID3" or (header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "mp3"
    if header[:4] == b"fLaC":
        return "flac"
    if header[:4] == b"OggS":
        return "ogg"
    if header[:4] == b"\x1a\x45\xdf\xa3":
        return "webm" if b"webm" in header[:_HEADER_BYTES] else "mkv"
    if header[4:8] == b"ftyp":
        return _ISOBMFF_BRANDS.get(header[8:12], "mp4")
    return None


def probe(path: Path) -> AudioInfo:
    """Read codec, sample rate, channels and duration from the first audio stream."""
    settings = get_settings()
    result = _run(
        [
            settings.ffprobe_binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioError("ffprobe returned no usable metadata") from exc

    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioError("file contains no audio stream")

    stream = streams[0]
    duration = _first_float(stream.get("duration"), payload.get("format", {}).get("duration"))
    if duration is None or duration <= 0:
        raise AudioError("audio stream has no measurable duration")

    return AudioInfo(
        codec=stream.get("codec_name", "unknown"),
        sample_rate=int(stream.get("sample_rate", 0)),
        channels=int(stream.get("channels", 0)),
        duration=duration,
    )


def normalize(source: Path, destination: Path) -> Path:
    """Transcode any input to 16 kHz mono 16-bit PCM WAV."""
    settings = get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ]
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise AudioError("normalization produced an empty file")
    return destination


def extract_chunk(source: Path, destination: Path, start: float, duration: float) -> Path:
    """Cut a slice of normalized audio, preserving the target WAV parameters."""
    settings = get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ]
    )
    return destination


def detect_silences(path: Path) -> list[tuple[float, float]]:
    """Return silent intervals reported by the ffmpeg silencedetect filter."""
    settings = get_settings()
    result = _run(
        [
            settings.ffmpeg_binary,
            "-nostdin",
            "-v",
            "info",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={settings.silence_noise_db}dB:d={settings.silence_min_duration}",
            "-f",
            "null",
            "-",
        ]
    )

    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in result.stderr.splitlines():
        if (start := _SILENCE_START.search(line)) is not None:
            pending = float(start.group(1))
        elif (end := _SILENCE_END.search(line)) is not None and pending is not None:
            silences.append((pending, float(end.group(1))))
            pending = None
    return silences


def plan_chunks(duration: float, silences: list[tuple[float, float]]) -> list[ChunkPlan]:
    """Split a timeline into overlapping chunks that end on silence where possible."""
    settings = get_settings()
    minimum = settings.chunk_min_seconds
    target = settings.chunk_target_seconds
    maximum = settings.chunk_max_seconds
    overlap = settings.chunk_overlap_seconds

    boundaries = sorted((start + end) / 2 for start, end in silences)
    plans: list[ChunkPlan] = []
    cursor = 0.0

    while cursor < duration - 0.05:
        if duration - cursor <= maximum:
            end = duration
        else:
            window = [b for b in boundaries if cursor + minimum <= b <= cursor + maximum]
            ideal = cursor + target
            if window:
                end = min(window, key=lambda b: abs(b - ideal))
            else:
                # Without a silence to cut on, avoid leaving a sliver as the final chunk.
                end = min(cursor + maximum, max(duration - minimum, cursor + minimum))

        plans.append(
            ChunkPlan(index=len(plans), start=round(cursor, 3), duration=round(end - cursor, 3))
        )
        if end >= duration:
            break
        cursor = max(end - overlap, cursor + 0.001)

    return plans


def _first_float(*values: object) -> float | None:
    for value in values:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    settings = get_settings()
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=settings.ffmpeg_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"{args[0]} is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"{args[0]} timed out after {settings.ffmpeg_timeout_seconds}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise AudioError(detail[-1] if detail else f"{args[0]} exited with {result.returncode}")
    return result
