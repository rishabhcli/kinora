"""Unit tests for deterministic coverage planning + render-mode mapping."""

from __future__ import annotations

from app.agents.contracts import RenderMode, SceneTempo
from app.video.storyboard import (
    CanonContext,
    PassageBeat,
    ShotCoverage,
    entities_for,
    plan_coverage,
    render_mode_for,
    speakers_in_beat,
)


def _beat(text: str, **kw: object) -> PassageBeat:
    base: dict[str, object] = {
        "beat_id": "b0",
        "text": text,
        "word_range": (0, len(text.split())),
    }
    base.update(kw)
    return PassageBeat(**base)


def test_ellipsis_collapses_to_single_transition() -> None:
    beat = _beat("The next morning the snow had stopped.", tempo=SceneTempo.ELLIPSIS)
    roles = plan_coverage(beat, CanonContext())
    assert roles == [ShotCoverage.TRANSITION]


def test_master_is_always_present() -> None:
    beat = _beat("Mara crossed the room.", entities=["mara"])
    roles = plan_coverage(beat, CanonContext(entities=["mara"]))
    assert ShotCoverage.MASTER in roles


def test_establishing_fires_for_first_beat_no_character() -> None:
    beat = _beat("The cold hall stretched away into shadow.", entities=[])
    ctx = CanonContext(entities=[], location="great_hall")
    roles = plan_coverage(beat, ctx, is_first=True)
    assert roles[0] is ShotCoverage.ESTABLISHING


def test_no_establishing_when_character_present() -> None:
    beat = _beat("Mara entered the hall.", entities=["mara"])
    ctx = CanonContext(entities=["mara"], locked_entities=["mara"], location="great_hall")
    roles = plan_coverage(beat, ctx, is_first=True)
    assert ShotCoverage.ESTABLISHING not in roles
    assert roles[0] is ShotCoverage.MASTER


def test_two_speakers_earn_a_reaction() -> None:
    text = '"Stop," said Mara. "Never," answered Tomas.'
    beat = _beat(text, entities=["mara", "tomas"])
    assert len(speakers_in_beat(beat)) >= 2
    roles = plan_coverage(beat, CanonContext(entities=["mara", "tomas"]))
    assert ShotCoverage.REACTION in roles


def test_subjective_beat_earns_pov() -> None:
    beat = _beat(
        "She felt the fear coil inside her.",
        entities=["mara"],
        subjective=True,
        pov_character="mara",
    )
    roles = plan_coverage(beat, CanonContext(entities=["mara"]))
    assert ShotCoverage.POV in roles


def test_pause_beat_earns_an_insert() -> None:
    beat = _beat("The sword lay still on the table.", entities=["mara"], tempo=SceneTempo.PAUSE)
    roles = plan_coverage(beat, CanonContext(entities=["mara"]))
    assert ShotCoverage.INSERT in roles


def test_render_mode_locks_character_when_reference_available() -> None:
    beat = _beat("Mara turned.", entities=["mara"])
    ctx = CanonContext(entities=["mara"], locked_entities=["mara"])
    assert render_mode_for(ShotCoverage.MASTER, beat, ctx) is RenderMode.REFERENCE_TO_VIDEO


def test_render_mode_falls_back_to_t2v_without_locked_ref() -> None:
    beat = _beat("Mara turned.", entities=["mara"])
    ctx = CanonContext(entities=["mara"], locked_entities=[])  # not locked yet
    assert render_mode_for(ShotCoverage.MASTER, beat, ctx) is RenderMode.TEXT_TO_VIDEO


def test_establishing_is_text_to_video() -> None:
    beat = _beat("The hall.", entities=[])
    assert render_mode_for(ShotCoverage.ESTABLISHING, beat, CanonContext()) is (
        RenderMode.TEXT_TO_VIDEO
    )


def test_entities_for_establishing_is_empty() -> None:
    beat = _beat("Mara entered.", entities=["mara"])
    assert entities_for(ShotCoverage.ESTABLISHING, beat, CanonContext(entities=["mara"])) == []


def test_entities_for_reaction_narrows_to_pov() -> None:
    beat = _beat(
        "Tomas spoke and Mara listened.",
        entities=["mara", "tomas"],
        pov_character="mara",
    )
    out = entities_for(ShotCoverage.REACTION, beat, CanonContext(entities=["mara", "tomas"]))
    assert out == ["mara"]


def test_entities_for_drops_pov_outside_beat() -> None:
    # A vantage character the beat never names is dropped (no-invent guardrail).
    beat = _beat("Tomas spoke.", entities=["tomas"], pov_character="ghost")
    out = entities_for(ShotCoverage.POV, beat, CanonContext(entities=["tomas"]))
    assert "ghost" not in out
    assert out == ["tomas"]
