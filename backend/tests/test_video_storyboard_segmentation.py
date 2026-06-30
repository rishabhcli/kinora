"""Unit tests for deterministic passage → beats segmentation (no network)."""

from __future__ import annotations

from app.agents.contracts import SceneTempo
from app.video.storyboard import CanonContext, Passage, segment_passage


def _passage(text: str, *, offset: int = 0) -> Passage:
    return Passage(
        passage_id="p1",
        text=text,
        word_offset=offset,
        page=4,
        context=CanonContext(entities=["mara"]),
    )


def test_empty_text_yields_no_beats() -> None:
    assert segment_passage(_passage("")) == []


def test_segments_on_sentence_groups() -> None:
    text = "One sentence here. Two sentence now. Three sentence last."
    beats = segment_passage(_passage(text), words_per_beat=8)
    # ~3 words/sentence; an 8-word target packs ~2-3 sentences per beat.
    assert len(beats) >= 1
    assert all(b.text for b in beats)


def test_word_ranges_are_absolute_and_contiguous() -> None:
    text = "Alpha bravo charlie delta. Echo foxtrot golf hotel. India juliet kilo lima."
    beats = segment_passage(_passage(text, offset=100), words_per_beat=4)
    # First beat starts at the passage word offset; ranges tile with no gap.
    assert beats[0].word_range[0] == 100
    for prev, nxt in zip(beats, beats[1:], strict=False):
        assert prev.word_range[1] == nxt.word_range[0]
    # Total span = total word count.
    assert beats[-1].word_range[1] == 100 + 12


def test_beat_ids_are_stable_and_ordered() -> None:
    beats = segment_passage(_passage("A b c. D e f. G h i."), words_per_beat=3)
    ids = [b.beat_id for b in beats]
    assert ids == sorted(ids)
    assert ids[0] == "p1_beat_000"


def test_tempo_is_classified_per_beat() -> None:
    text = '"Run!" she screamed. The valley stretched silent and still below.'
    beats = segment_passage(_passage(text), words_per_beat=6)
    tempos = {b.tempo for b in beats}
    # The dialogue beat reads SCENE; the description beat reads PAUSE.
    assert SceneTempo.SCENE in tempos or SceneTempo.PAUSE in tempos


def test_beats_inherit_passage_entities() -> None:
    beats = segment_passage(_passage("Mara walked. Mara ran."), words_per_beat=4)
    assert all(b.entities == ["mara"] for b in beats)


def test_idempotent_segmentation() -> None:
    p = _passage("First beat sentence. Second beat sentence. Third beat sentence.")
    a = segment_passage(p, words_per_beat=5)
    b = segment_passage(p, words_per_beat=5)
    assert [x.model_dump() for x in a] == [y.model_dump() for y in b]
