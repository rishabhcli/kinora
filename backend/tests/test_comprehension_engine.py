"""Unit tests for the comprehension engine that composes every pass (no network).

``analyze_beat`` fills the per-beat literary fields; ``enrich_sequence`` adds the
book-level story-time; ``shot_intent`` distils a creative brief. The §10 no-invent
guardrail is enforced through ``canon_names``.
"""

from __future__ import annotations

from app.agents.comprehension import (
    analyze_beat,
    build_shot_intent,
    enrich_sequence,
    shot_intent,
)
from app.agents.contracts import (
    Beat,
    DiscourseMode,
    NarrativePerson,
    SceneTempo,
    ShotIntent,
    TimePosition,
)


def _beat(beat_id: str, index: int, summary: str, visuals: str = "") -> Beat:
    return Beat(beat_id=beat_id, beat_index=index, summary=summary, described_visuals=visuals)


def test_analyze_beat_fills_all_fields() -> None:
    beat = _beat(
        "b0",
        0,
        '"We must flee!" cried Elsa, and the wind howled like a wounded beast.',
        "Elsa at the castle gate in a storm",
    )
    out = analyze_beat(beat, canon_names={"Elsa"})
    # Dialogue diarized and attributed.
    assert out.dialogue and out.dialogue[0].speaker == "Elsa"
    # A figure of speech was detected and translated.
    assert out.devices
    # Pacing: a dialogue/action beat is a SCENE.
    assert out.tempo is SceneTempo.SCENE
    # Discourse is dialogue-dominant or narration (not interiority).
    assert out.discourse in (DiscourseMode.DIALOGUE, DiscourseMode.NARRATION)


def test_analyze_beat_interiority() -> None:
    beat = _beat("b1", 1, "She wondered if she had ever truly been free.")
    out = analyze_beat(beat)
    assert out.discourse is DiscourseMode.INTERIOR_MONOLOGUE
    assert out.interiority is not None


def test_analyze_beat_empty_returns_unchanged() -> None:
    beat = Beat(beat_id="b2", summary="")
    out = analyze_beat(beat)
    assert out.discourse is DiscourseMode.NARRATION
    assert out.tempo is SceneTempo.SCENE


def test_enrich_sequence_reconstructs_story_time() -> None:
    beats = [
        _beat("b0", 0, "She sat alone in the cottage now."),
        _beat("b1", 1, "Years before, the village had burned to the ground."),
        _beat("b2", 2, "She rose and returned to the present, stoking the fire."),
    ]
    out = enrich_sequence(beats)
    by_id = {b.beat_id: b for b in out}
    # Narrative order preserved; story-time diverges on the flashback.
    assert [b.story_time.narrative_order for b in out] == [0, 1, 2]
    assert by_id["b1"].story_time.position is TimePosition.FLASHBACK
    assert by_id["b1"].story_time.order < by_id["b0"].story_time.order


def test_enrich_sequence_pov_filtering() -> None:
    beats = [_beat("b0", 0, "Elsa knew the truth. Elsa felt the cold settle in.")]
    out = enrich_sequence(beats, canon_names={"Elsa"})
    assert out[0].pov is NarrativePerson.THIRD_LIMITED
    assert out[0].pov_character == "Elsa"


def test_shot_intent_distils_brief() -> None:
    beat = _beat(
        "b0", 0, "She wondered, lost in memory, if grief was a stone she carried."
    )
    out = analyze_beat(beat)
    brief = shot_intent(out)
    # Interiority ⇒ subjective staging instruction present.
    assert "SUBJECTIVE" in brief


def test_build_shot_intent_structured() -> None:
    beat = _beat(
        "b0",
        0,
        '"Run!" cried Elsa. The wind howled like a wounded animal as she fled.',
    )
    out = analyze_beat(beat, canon_names={"Elsa"})
    intent = build_shot_intent(out)
    assert isinstance(intent, ShotIntent)
    assert "Elsa" in intent.speakers
    assert intent.visual_motifs  # the simile produced a motif
    assert intent.pacing  # a pacing hint is set
    assert intent.brief  # the assembled brief is non-empty


def test_build_shot_intent_unreliable_subjective() -> None:
    beat = _beat(
        "b1",
        1,
        "I swear I never touched it. Perhaps. Honestly, I could have sworn the "
        "door was locked, or so I thought, I suppose.",
    )
    out = analyze_beat(beat)
    intent = build_shot_intent(out)
    assert intent.unreliable is True
    assert "unreliable" in intent.brief.lower()


def test_empty_beat_yields_neutral_intent() -> None:
    intent = build_shot_intent(Beat(beat_id="b2", summary=""))
    assert intent.subjective is False
    assert intent.speakers == []
    assert intent.visual_motifs == []
