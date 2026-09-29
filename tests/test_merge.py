from app.engines.base import Segment
from app.services.merge import merge_chunk_segments, segments_to_text, to_dicts


def segment(start: float, end: float, text: str, confidence: float = 0.9) -> dict:
    return {"start": start, "end": end, "text": text, "confidence": confidence}


def test_to_dicts_numbers_segments() -> None:
    result = to_dicts([Segment(0.0, 1.0, " hello ", 0.5), Segment(1.0, 2.0, "world", 0.6)])

    assert [item["id"] for item in result] == [0, 1]
    assert result[0]["text"] == "hello"


def test_merge_applies_chunk_offsets() -> None:
    merged = merge_chunk_segments(
        [
            (0.0, [segment(0.0, 4.0, "first chunk speech")]),
            (44.0, [segment(1.0, 5.0, "second chunk speech")]),
        ]
    )

    assert [item["start"] for item in merged] == [0.0, 45.0]
    assert [item["end"] for item in merged] == [4.0, 49.0]
    assert [item["id"] for item in merged] == [0, 1]


def test_merge_orders_chunks_by_offset() -> None:
    merged = merge_chunk_segments(
        [
            (60.0, [segment(0.0, 2.0, "later")]),
            (0.0, [segment(0.0, 2.0, "earlier")]),
        ]
    )

    assert [item["text"] for item in merged] == ["earlier", "later"]


def test_merge_trims_words_repeated_in_the_overlap() -> None:
    merged = merge_chunk_segments(
        [
            (0.0, [segment(0.0, 45.0, "the meeting starts at noon sharp")]),
            (44.0, [segment(0.0, 6.0, "at noon sharp we review the roadmap")]),
        ]
    )

    assert [item["text"] for item in merged] == [
        "the meeting starts at noon sharp",
        "we review the roadmap",
    ]
    assert merged[1]["start"] > 44.0


def test_merge_drops_a_segment_duplicated_entirely() -> None:
    merged = merge_chunk_segments(
        [
            (0.0, [segment(0.0, 45.0, "closing remarks follow")]),
            (
                44.0,
                [segment(0.0, 1.0, "Closing remarks, follow!"), segment(1.0, 3.0, "next topic")],
            ),
        ]
    )

    assert [item["text"] for item in merged] == ["closing remarks follow", "next topic"]
    assert [item["id"] for item in merged] == [0, 1]


def test_merge_keeps_distinct_speech_inside_the_overlap() -> None:
    merged = merge_chunk_segments(
        [
            (0.0, [segment(0.0, 45.0, "alpha beta")]),
            (44.0, [segment(0.0, 2.0, "gamma delta")]),
        ]
    )

    assert [item["text"] for item in merged] == ["alpha beta", "gamma delta"]


def test_merge_skips_empty_segments() -> None:
    merged = merge_chunk_segments([(0.0, [segment(0.0, 1.0, "   "), segment(1.0, 2.0, "kept")])])

    assert [item["text"] for item in merged] == ["kept"]


def test_segments_to_text_joins_in_order() -> None:
    assert segments_to_text([segment(0, 1, "one"), segment(1, 2, "two")]) == "one two"
