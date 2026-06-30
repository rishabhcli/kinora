"""Backend-agnostic timing ingest (kinora.md §9.4): every provider timing shape —
per-word, per-char, per-token, SRT/VTT cues — normalizes into one canonical
``WordTiming`` timeline, plus shape sniffing and unit conversion. Pure: no
ffmpeg/DB/network."""

from __future__ import annotations

import pytest

from app.providers.types import TtsWord
from app.video.sync.ingest import ingest_timings, sniff_shape, words_from_cue
from app.video.sync.models import RawCue, TimingShape

# --------------------------------------------------------------------------- #
# per-word
# --------------------------------------------------------------------------- #


def test_per_word_mappings_pass_through() -> None:
    raw = [
        {"text": "She", "t_start": 0.1, "t_end": 0.32},
        {"text": "stood", "t_start": 0.32, "t_end": 0.61},
    ]
    out = ingest_timings(raw, shape=TimingShape.PER_WORD)
    assert [(w.text, w.t_start, w.t_end) for w in out] == [
        ("She", 0.1, 0.32),
        ("stood", 0.32, 0.61),
    ]
    assert all(not w.estimated for w in out)


def test_per_word_accepts_objects_like_ttsword() -> None:
    raw = [TtsWord(text="Hi", t_start=0.0, t_end=0.4)]
    out = ingest_timings(raw, shape=TimingShape.PER_WORD)
    assert (out[0].text, out[0].t_start, out[0].t_end) == ("Hi", 0.0, 0.4)


def test_per_word_alt_field_names_and_duration() -> None:
    # start + duration instead of start + end, alternate field spellings.
    raw = [{"word": "go", "begin": 1.0, "duration": 0.5}]
    out = ingest_timings(raw, shape=TimingShape.PER_WORD)
    assert (out[0].text, out[0].t_start, out[0].t_end) == ("go", 1.0, 1.5)


def test_per_word_skips_blank_tokens() -> None:
    raw = [
        {"text": "  ", "t_start": 0.0, "t_end": 0.1},
        {"text": "x", "t_start": 0.1, "t_end": 0.2},
    ]
    out = ingest_timings(raw, shape=TimingShape.PER_WORD)
    assert [w.text for w in out] == ["x"]


# --------------------------------------------------------------------------- #
# per-char
# --------------------------------------------------------------------------- #


def test_per_char_coalesces_into_words_on_whitespace() -> None:
    chars = [
        {"char": "H", "start": 0.0, "end": 0.1},
        {"char": "i", "start": 0.1, "end": 0.2},
        {"char": " ", "start": 0.2, "end": 0.22},
        {"char": "y", "start": 0.22, "end": 0.3},
        {"char": "o", "start": 0.3, "end": 0.4},
        {"char": "u", "start": 0.4, "end": 0.5},
    ]
    out = ingest_timings(chars, shape=TimingShape.PER_CHAR)
    assert [(w.text, w.t_start, w.t_end) for w in out] == [
        ("Hi", 0.0, 0.2),
        ("you", 0.22, 0.5),
    ]


def test_per_char_emits_trailing_word_without_final_space() -> None:
    chars = [{"char": "a", "start": 0.0, "end": 0.5}]
    out = ingest_timings(chars, shape=TimingShape.PER_CHAR)
    assert [(w.text, w.t_end) for w in out] == [("a", 0.5)]


# --------------------------------------------------------------------------- #
# per-token (sub-word pieces re-joined into words)
# --------------------------------------------------------------------------- #


def test_per_token_sentencepiece_leading_space() -> None:
    tok = [
        {"token": "▁Hel", "start": 0.0, "end": 0.2},  # ▁Hel
        {"token": "lo", "start": 0.2, "end": 0.4},
        {"token": "▁world", "start": 0.4, "end": 0.8},
    ]
    out = ingest_timings(tok, shape=TimingShape.PER_TOKEN)
    assert [(w.text, w.t_start, w.t_end) for w in out] == [
        ("Hello", 0.0, 0.4),
        ("world", 0.4, 0.8),
    ]


def test_per_token_bert_wordpiece_continuation() -> None:
    tok = [
        {"token": "play", "start": 0.0, "end": 0.3},
        {"token": "##ing", "start": 0.3, "end": 0.6},
    ]
    out = ingest_timings(tok, shape=TimingShape.PER_TOKEN)
    assert [(w.text, w.t_start, w.t_end) for w in out] == [("playing", 0.0, 0.6)]


# --------------------------------------------------------------------------- #
# cues (SRT/VTT phrases)
# --------------------------------------------------------------------------- #


def test_cue_distributes_words_inside_window() -> None:
    cues = [{"text": "Hello world there", "start": 0.0, "end": 3.0}]
    out = ingest_timings(cues, shape=TimingShape.CUE)
    assert [w.text for w in out] == ["Hello", "world", "there"]
    # contiguous, ends exactly at the cue end, monotonic
    assert out[0].t_start == 0.0
    assert out[-1].t_end == 3.0
    for a, b in zip(out, out[1:], strict=False):
        assert a.t_end == pytest.approx(b.t_start)


def test_words_from_cue_punctuation_pause_lengthens_clause() -> None:
    # The word ending in a comma should get a noticeably longer span than its
    # syllable-count alone would imply (the pause folds into it).
    cue = RawCue(text="wait, go", t_start=0.0, t_end=2.0)
    out = words_from_cue(cue)
    assert [w.text for w in out] == ["wait,", "go"]
    assert out[0].duration > out[1].duration


def test_multiple_cues_chain_in_order() -> None:
    cues = [
        RawCue(text="one two", t_start=0.0, t_end=1.0),
        RawCue(text="three", t_start=1.0, t_end=2.0),
    ]
    out = ingest_timings(cues, shape=TimingShape.CUE)
    assert [w.text for w in out] == ["one", "two", "three"]
    assert out[-1].t_end == 2.0


# --------------------------------------------------------------------------- #
# none → estimator path
# --------------------------------------------------------------------------- #


def test_none_shape_estimates_from_text_and_duration() -> None:
    out = ingest_timings(None, text="She stood still", duration_s=3.0)
    assert [w.text for w in out] == ["She", "stood", "still"]
    assert all(w.estimated for w in out)
    assert out[-1].t_end == 3.0


def test_none_shape_without_text_or_duration_raises() -> None:
    with pytest.raises(ValueError, match="cannot estimate"):
        ingest_timings(None)
    with pytest.raises(ValueError, match="cannot estimate"):
        ingest_timings([], text="hi", duration_s=0.0)


def test_empty_raw_with_explicit_word_shape_falls_back_to_estimate() -> None:
    # An empty payload always needs text+duration regardless of declared shape.
    out = ingest_timings([], shape=TimingShape.PER_WORD, text="hi there", duration_s=2.0)
    assert [w.text for w in out] == ["hi", "there"]
    assert all(w.estimated for w in out)


# --------------------------------------------------------------------------- #
# unit conversion (ms)
# --------------------------------------------------------------------------- #


def test_ms_unit_scales_to_seconds() -> None:
    raw = [{"text": "hi", "start": 500, "end": 900}]
    out = ingest_timings(raw, shape=TimingShape.PER_WORD, unit="ms")
    assert (out[0].t_start, out[0].t_end) == (0.5, 0.9)


# --------------------------------------------------------------------------- #
# sniffing
# --------------------------------------------------------------------------- #


def test_sniff_empty_is_none() -> None:
    assert sniff_shape([]) is TimingShape.NONE
    assert sniff_shape(None) is TimingShape.NONE


def test_sniff_phrase_is_cue() -> None:
    assert sniff_shape([{"text": "two words", "start": 0, "end": 1}]) is TimingShape.CUE


def test_sniff_single_chars_is_per_char() -> None:
    chars = [{"char": c, "start": 0, "end": 1} for c in "abc"] + [
        {"char": " ", "start": 1, "end": 1}
    ]
    assert sniff_shape(chars) is TimingShape.PER_CHAR


def test_sniff_sentinel_token_is_per_token() -> None:
    tok = [{"token": "▁Hi", "start": 0, "end": 1}]
    assert sniff_shape(tok) is TimingShape.PER_TOKEN


def test_sniff_plain_words_is_per_word() -> None:
    raw = [{"text": "Hello", "t_start": 0, "t_end": 1}, {"text": "world", "t_start": 1, "t_end": 2}]
    assert sniff_shape(raw) is TimingShape.PER_WORD
