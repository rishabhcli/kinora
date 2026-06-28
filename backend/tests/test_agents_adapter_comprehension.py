"""Adapter ↔ comprehension-engine integration (no network).

Verifies that ``analyze_page`` runs the deep-comprehension passes on each beat,
that ``comprehend_sequence`` reconstructs story-time across pages, and that
``plan_shots`` is pacing-aware (a SUMMARY beat yields fewer/longer shots than a
dramatised SCENE beat over the same word span), while a neutral SCENE beat still
reproduces the legacy split exactly.
"""

from __future__ import annotations

from app.agents.adapter import SHOT_SECONDS, WORDS_PER_SHOT, Adapter
from app.agents.contracts import (
    Beat,
    DiscourseMode,
    NarrativePerson,
    SceneTempo,
    SourceSpan,
    TimePosition,
)
from app.providers import Providers
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)

_DIALOGUE_PAGE = {
    "beats": [
        {
            "summary": '"We must flee the castle now!" cried Elsa to Anna.',
            "entities": ["Elsa", "Anna"],
            "unresolved_entities": [],
            "described_visuals": "two sisters at the castle gate",
            "mood": "urgent",
            "source_span": {"page": 1, "para": 1, "word_range": [0, 30]},
        }
    ]
}


async def test_analyze_page_comprehends_each_beat(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer(_DIALOGUE_PAGE)  # type: ignore[method-assign]
    adapter = Adapter(providers)

    beats = await adapter.analyze_page(
        "...", page=1, scene_id="scene_001", known_entities={"Elsa", "Anna"}
    )

    beat = beats[0]
    # The comprehension passes populated the literary fields.
    assert beat.tempo is SceneTempo.SCENE  # dialogue/action → dramatised scene
    assert beat.dialogue and beat.dialogue[0].speaker == "Elsa"
    assert beat.discourse in (DiscourseMode.DIALOGUE, DiscourseMode.NARRATION)


async def test_analyze_page_comprehend_off(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer(_DIALOGUE_PAGE)  # type: ignore[method-assign]
    adapter = Adapter(providers)
    beats = await adapter.analyze_page("...", page=1, comprehend=False)
    # With comprehension off the beat keeps the neutral defaults.
    assert beats[0].tempo is SceneTempo.SCENE
    assert beats[0].dialogue == []


def test_comprehend_sequence_reconstructs_story_time(providers: Providers) -> None:  # noqa: F811
    beats = [
        Beat(beat_id="b0", beat_index=0, summary="She sat by the hearth now."),
        Beat(
            beat_id="b1",
            beat_index=1,
            summary="Years before, the village had burned in the night.",
        ),
        Beat(beat_id="b2", beat_index=2, summary="She returned to the present moment."),
    ]
    out = Adapter(providers).comprehend_sequence(beats)
    by_id = {b.beat_id: b for b in out}
    assert by_id["b1"].story_time.position is TimePosition.FLASHBACK
    # Story-time ranks the flashback before the present beats; narrative order kept.
    assert by_id["b1"].story_time.order < by_id["b0"].story_time.order
    assert [b.story_time.narrative_order for b in out] == [0, 1, 2]


def test_plan_shots_scene_matches_legacy(providers: Providers) -> None:  # noqa: F811
    """A neutral SCENE beat reproduces the original split exactly (regression)."""
    beat = Beat(
        beat_id="beat_0001",
        summary="action",
        tempo=SceneTempo.SCENE,
        source_span=SourceSpan(word_range=(0, 3 * WORDS_PER_SHOT)),
    )
    shots = Adapter(providers).plan_shots([beat])
    assert len(shots) == 3
    assert all(s.est_duration_s == SHOT_SECONDS for s in shots)
    assert shots[0].source_span.word_range == (0, WORDS_PER_SHOT)


def test_plan_shots_pacing_aware_density(providers: Providers) -> None:  # noqa: F811
    """A SUMMARY beat packs the SAME span into fewer, not-shorter shots than a SCENE."""
    span = SourceSpan(word_range=(0, 3 * WORDS_PER_SHOT))
    scene_beat = Beat(beat_id="b_scene", summary="x", tempo=SceneTempo.SCENE, source_span=span)
    summary_beat = Beat(
        beat_id="b_summary", summary="x", tempo=SceneTempo.SUMMARY, source_span=span
    )
    adapter = Adapter(providers)
    scene_shots = adapter.plan_shots([scene_beat])
    summary_shots = adapter.plan_shots([summary_beat])
    # Same 180-word span: the summary is compressed into fewer clips.
    assert len(summary_shots) < len(scene_shots)


def test_plan_shots_attaches_intent(providers: Providers) -> None:  # noqa: F811
    """Each shot carries its beat's comprehension-derived staging intent."""
    beat = analyze_dialogue_beat()
    shots = Adapter(providers).plan_shots([beat])
    assert shots
    intent = shots[0].intent
    assert "Elsa" in intent.speakers
    assert intent.brief  # a non-empty natural-language brief flows onto the shot


def analyze_dialogue_beat() -> Beat:
    from app.agents.comprehension import analyze_beat

    raw = Beat(
        beat_id="b0",
        summary='"We must flee!" cried Elsa as the wind howled like a beast.',
        source_span=SourceSpan(word_range=(0, 30)),
    )
    return analyze_beat(raw, canon_names={"Elsa"})


def test_plan_shots_pause_lingers(providers: Providers) -> None:  # noqa: F811
    """A PAUSE beat's shots run longer (held) than the same span as a SCENE."""
    span = SourceSpan(word_range=(0, 48))  # below the anchor so bias is visible
    scene = Beat(beat_id="b_s", summary="x", tempo=SceneTempo.SCENE, source_span=span)
    pause = Beat(beat_id="b_p", summary="x", tempo=SceneTempo.PAUSE, source_span=span)
    adapter = Adapter(providers)
    scene_dur = adapter.plan_shots([scene])[0].est_duration_s
    pause_dur = adapter.plan_shots([pause])[0].est_duration_s
    assert pause_dur > scene_dur


async def test_pov_flows_through_analyze_page(providers: Providers) -> None:  # noqa: F811
    page = {
        "beats": [
            {
                "summary": "Elsa knew the storm was her doing. Elsa felt the ice spread.",
                "entities": ["Elsa"],
                "source_span": {"page": 1, "word_range": [0, 20]},
            }
        ]
    }
    providers.chat.chat_json = JsonSequencer(page)  # type: ignore[method-assign]
    beats = await Adapter(providers).analyze_page("...", page=1, known_entities={"Elsa"})
    assert beats[0].pov is NarrativePerson.THIRD_LIMITED
    assert beats[0].pov_character == "Elsa"
