import json

from app.services.export import render, to_srt, to_vtt

SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 2.5, "text": "hello there", "confidence": 0.91},
    {"id": 1, "start": 2.5, "end": 3661.125, "text": " second line ", "confidence": 0.88},
]


def test_srt_uses_comma_separated_milliseconds_and_one_based_indexes() -> None:
    assert to_srt(SEGMENTS) == (
        "1\n"
        "00:00:00,000 --> 00:00:02,500\n"
        "hello there\n"
        "\n"
        "2\n"
        "00:00:02,500 --> 01:01:01,125\n"
        "second line\n"
    )


def test_vtt_has_a_header_and_dot_separated_milliseconds() -> None:
    assert to_vtt(SEGMENTS) == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:02.500\n"
        "hello there\n"
        "\n"
        "00:00:02.500 --> 01:01:01.125\n"
        "second line\n"
    )


def test_empty_transcripts_render_without_blocks() -> None:
    assert to_srt([]) == ""
    assert to_vtt([]) == "WEBVTT\n\n"


def test_json_export_round_trips_the_payload() -> None:
    payload = {"job_id": "abc", "status": "completed", "segments": SEGMENTS}
    assert json.loads(render("json", payload)) == payload


def test_render_dispatches_on_format() -> None:
    payload = {"segments": SEGMENTS}
    assert render("srt", payload).startswith("1\n")
    assert render("vtt", payload).startswith("WEBVTT")
