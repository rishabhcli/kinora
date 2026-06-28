"""Unit tests for the book-level comprehension report (no network)."""

from __future__ import annotations

from app.agents.comprehension import enrich_sequence, summarize_comprehension
from app.agents.comprehension.report import dominant_discourse, dominant_tempo
from app.agents.contracts import Beat, DiscourseMode, SceneTempo


def _beat(i: int, summary: str) -> Beat:
    return Beat(beat_id=f"b{i}", beat_index=i, summary=summary)


def test_report_single_pov_linear() -> None:
    beats = enrich_sequence(
        [
            _beat(0, "Elsa woke. Elsa felt the cold settle in her bones."),
            _beat(1, "Elsa crossed the courtyard and opened the gate."),
        ],
        canon_names={"Elsa"},
    )
    report = summarize_comprehension(beats)
    assert report.num_beats == 2
    assert report.linear is True
    assert report.flashback_beats == 0
    assert report.multi_pov is False
    assert "Elsa" in report.pov_characters


def test_report_detects_multi_pov() -> None:
    beats = enrich_sequence(
        [
            _beat(0, "I walked into the cold hall, my breath clouding before me."),
            _beat(1, "Elsa knew the storm was hers. Elsa felt the ice spread."),
        ],
        canon_names={"Elsa"},
    )
    report = summarize_comprehension(beats)
    # A first-person beat and a third-limited beat ⇒ multi-POV.
    assert report.multi_pov is True


def test_report_detects_nonlinear() -> None:
    beats = enrich_sequence(
        [
            _beat(0, "She sat by the fire now."),
            _beat(1, "Years before, the village had burned to ash."),
            _beat(2, "She returned to the present and stoked the embers."),
        ]
    )
    report = summarize_comprehension(beats)
    assert report.linear is False
    assert report.flashback_beats == 1


def test_report_counts_dialogue_and_devices() -> None:
    beats = enrich_sequence(
        [_beat(0, '"Run!" cried Elsa as the wind howled like a wounded beast.')],
        canon_names={"Elsa"},
    )
    report = summarize_comprehension(beats)
    assert report.total_dialogue_lines >= 1
    assert report.attributed_dialogue_lines >= 1
    assert report.total_devices >= 1


def test_dominant_tempo_and_discourse() -> None:
    beats = enrich_sequence(
        [
            _beat(0, '"Run!" she screamed and bolted for the door.'),
            _beat(1, '"Wait!" he called, lunging after her.'),
            _beat(2, "The war dragged on for many years across the broken land."),
        ]
    )
    report = summarize_comprehension(beats)
    # Two dramatised scenes vs one summary ⇒ SCENE dominates.
    assert dominant_tempo(report) is SceneTempo.SCENE
    assert dominant_discourse(report) in (DiscourseMode.DIALOGUE, DiscourseMode.NARRATION)


def test_empty_report() -> None:
    report = summarize_comprehension([])
    assert report.num_beats == 0
    assert report.linear is True
    assert dominant_tempo(report) is SceneTempo.SCENE
