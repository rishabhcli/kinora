"""Caption export from the sync map (kinora.md §3 accessibility): cue packing
with char/word/duration limits + sentence-end breaks, and WebVTT/SRT serialisation.
Pure — no model, no I/O."""

from __future__ import annotations

from app.render.captions import (
    build_cues,
    segments_to_srt,
    segments_to_webvtt,
    to_srt,
    to_webvtt,
)
from app.render.sync_map import SyncSegment, SyncWord


def _words(*specs: tuple[str, float, float]) -> list[SyncWord]:
    return [
        SyncWord(word_index=i, text=t, t_start=a, t_end=b)
        for i, (t, a, b) in enumerate(specs)
    ]


def _seg(words: list[SyncWord], *, start: float = 0.0, end: float = 10.0) -> SyncSegment:
    return SyncSegment(
        shot_id="s", video_start_s=start, video_end_s=end, page=1, page_turn_at_s=end - 0.2,
        words=words,
    )


def test_cue_breaks_on_sentence_end() -> None:
    seg = _seg(
        _words(("She", 0.0, 0.5), ("ran.", 0.5, 1.0), ("Then", 1.0, 1.5), ("stopped.", 1.5, 2.0))
    )
    cues = build_cues([seg])
    assert len(cues) == 2
    assert cues[0].text == "She ran."
    assert cues[1].text == "Then stopped."
    assert cues[0].t_start == 0.0 and cues[0].t_end == 1.0


def test_cue_breaks_on_word_limit() -> None:
    words = _words(*[(f"w{i}", i * 0.3, i * 0.3 + 0.3) for i in range(20)])
    cues = build_cues([_seg(words, end=10.0)])
    # No sentence ends → broken by the 9-word cap.
    assert all(len(c.text.split()) <= 9 for c in cues)
    assert len(cues) >= 2


def test_cue_breaks_on_duration_limit() -> None:
    # Two slow words spanning > 6s force a duration break.
    words = _words(("aaa", 0.0, 4.0), ("bbb", 4.0, 8.0))
    cues = build_cues([_seg(words, end=8.0)])
    assert all(c.t_end - c.t_start <= 6.0 + 0.01 for c in cues)


def test_cues_flow_across_segments() -> None:
    a = _seg(_words(("Once", 0.0, 0.5), ("upon", 0.5, 1.0)), end=1.0)
    # A merged scene's second segment carries scene-absolute (shifted) word times.
    b_shifted = _seg(_words(("a", 1.0, 1.3), ("time.", 1.3, 1.8)), start=1.0, end=2.0)
    cues = build_cues([a, b_shifted])
    assert len(cues) == 1  # one sentence across two shots
    assert cues[0].text == "Once upon a time."


def test_webvtt_format() -> None:
    cues = build_cues([_seg(_words(("Hi.", 0.0, 1.0)))])
    vtt = to_webvtt(cues)
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in vtt
    assert "Hi." in vtt


def test_srt_format() -> None:
    cues = build_cues([_seg(_words(("Hi.", 0.0, 1.5)))])
    srt = to_srt(cues)
    assert srt.startswith("1\n")
    assert "00:00:00,000 --> 00:00:01,500" in srt  # SRT uses a comma separator


def test_timestamp_hours_minutes() -> None:
    cues = build_cues([_seg(_words(("late.", 3725.0, 3725.5)), end=3726.0)])
    vtt = to_webvtt(cues)
    # 3725s = 01:02:05.
    assert "01:02:05.000" in vtt


def test_empty_segments_yield_empty_documents() -> None:
    assert build_cues([]) == []
    assert to_srt([]) == ""
    assert to_webvtt([]).strip() == "WEBVTT"


def test_segments_to_format_convenience() -> None:
    seg = _seg(_words(("Hello.", 0.0, 1.0)))
    assert "Hello." in segments_to_webvtt([seg])
    assert "Hello." in segments_to_srt([seg])
