import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.engines import TranscriptionError, get_engine
from app.services import audio
from app.services.merge import merge_chunk_segments, segments_to_text, to_dicts

_HEADER_BYTES = 64


def transcribe_file(path: Path, language: str | None = None) -> dict[str, Any]:
    """Run the full local pipeline on one file and return the transcript payload."""
    if not path.is_file():
        raise audio.AudioError(f"{path} is not a file")

    with path.open("rb") as handle:
        media_format = audio.detect_format(handle.read(_HEADER_BYTES))
    if media_format is None:
        raise audio.AudioError("unsupported or unrecognised media container")

    settings = get_settings()
    engine = get_engine()

    with tempfile.TemporaryDirectory(prefix="cli-transcribe-") as workdir:
        root = Path(workdir)
        audio.probe(path)
        normalized = audio.normalize(path, root / "normalized.wav")
        info = audio.probe(normalized)

        if info.duration <= settings.chunk_threshold_seconds:
            result = engine.transcribe(normalized, language)
            segments = to_dicts(result.segments)
            detected = result.language
        else:
            transcribed: list[tuple[float, list[dict[str, Any]]]] = []
            detected = None
            for plan in audio.plan_chunks(info.duration, audio.detect_silences(normalized)):
                piece = audio.extract_chunk(
                    normalized, root / f"chunk-{plan.index:05d}.wav", plan.start, plan.duration
                )
                result = engine.transcribe(piece, language)
                detected = detected or result.language
                transcribed.append((plan.start, to_dicts(result.segments)))
                piece.unlink(missing_ok=True)
            segments = merge_chunk_segments(transcribed)

    return {
        "file": str(path),
        "format": media_format,
        "language": detected,
        "duration": round(info.duration, 3),
        "full_text": segments_to_text(segments),
        "segments": segments,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    transcribe = subparsers.add_parser("transcribe", help="transcribe a local media file")
    transcribe.add_argument("file", type=Path)
    transcribe.add_argument(
        "--language", default=None, help="force a language instead of detecting"
    )

    args = parser.parse_args(argv)
    try:
        payload = transcribe_file(args.file, args.language)
    except (audio.AudioError, TranscriptionError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
