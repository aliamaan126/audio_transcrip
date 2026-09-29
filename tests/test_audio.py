import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import audio
from tests.conftest import SAMPLE_MP3, SAMPLE_WAV

ffmpeg_required = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are not installed",
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
        (b"RIFF\x00\x00\x00\x00AVI LIST", "avi"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00", "mp3"),
        (b"\xff\xfb\x90\xc4" + bytes(12), "mp3"),
        (b"fLaC\x00\x00\x00\x22" + bytes(8), "flac"),
        (b"OggS\x00\x02\x00\x00" + bytes(8), "ogg"),
        (b"\x1a\x45\xdf\xa3" + bytes(20) + b"webm" + bytes(8), "webm"),
        (b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00", "m4a"),
        (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", "mp4"),
    ],
)
def test_detect_format_reads_magic_bytes(header: bytes, expected: str) -> None:
    assert audio.detect_format(header) == expected


@pytest.mark.parametrize(
    "header",
    [b"", b"not audio", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d"],
)
def test_detect_format_rejects_unsupported_content(header: bytes) -> None:
    assert audio.detect_format(header) is None


def test_detect_format_ignores_extension() -> None:
    disguised = SAMPLE_WAV.read_bytes()[:64]
    assert audio.detect_format(disguised) == "wav"
    assert audio.detect_format(b"RIFF\x00\x00\x00\x00WEBP" + bytes(8)) is None


@ffmpeg_required
def test_probe_reports_stream_properties() -> None:
    info = audio.probe(SAMPLE_WAV)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.duration == pytest.approx(6.0, abs=0.05)


@ffmpeg_required
def test_normalize_produces_16khz_mono_pcm(tmp_path: Path) -> None:
    output = audio.normalize(SAMPLE_MP3, tmp_path / "normalized.wav")
    info = audio.probe(output)

    assert info.sample_rate == audio.TARGET_SAMPLE_RATE
    assert info.channels == audio.TARGET_CHANNELS
    assert info.codec == "pcm_s16le"


@ffmpeg_required
def test_probe_rejects_corrupt_audio(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32)

    with pytest.raises(audio.AudioError):
        audio.probe(corrupt)


@ffmpeg_required
def test_detect_silences_finds_the_quiet_gaps() -> None:
    silences = audio.detect_silences(SAMPLE_WAV)
    assert len(silences) >= 2
    assert silences[0][0] == pytest.approx(2.0, abs=0.2)


def test_plan_chunks_cuts_on_silence_and_overlaps() -> None:
    settings = get_settings()
    plans = audio.plan_chunks(200.0, [(44.0, 45.0), (90.0, 91.0), (140.0, 141.0)])

    assert [plan.index for plan in plans] == list(range(len(plans)))
    assert plans[0].start == 0.0
    assert plans[0].duration == pytest.approx(44.5)

    for previous, current in zip(plans, plans[1:], strict=False):
        previous_end = previous.start + previous.duration
        assert current.start == pytest.approx(previous_end - settings.chunk_overlap_seconds)
        assert current.start > previous.start

    last = plans[-1]
    assert last.start + last.duration == pytest.approx(200.0)


def test_plan_chunks_keeps_durations_within_bounds() -> None:
    settings = get_settings()
    plans = audio.plan_chunks(1000.0, [])

    assert all(plan.duration <= settings.chunk_max_seconds + 0.001 for plan in plans)
    assert all(plan.duration >= settings.chunk_min_seconds - 0.001 for plan in plans)


def test_plan_chunks_returns_single_chunk_for_short_audio() -> None:
    plans = audio.plan_chunks(30.0, [])
    assert len(plans) == 1
    assert plans[0].start == 0.0
    assert plans[0].duration == pytest.approx(30.0)
