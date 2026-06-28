"""Unit tests for the optional LLM-refinement merge + the Adapter pass (no network).

The merge policy (:func:`merge_comprehension`) is tested purely; the Adapter's
``enrich_beat_llm`` is tested with a canned JSON stand-in so no DashScope call is
made — verifying the heuristic floor, the conservative merge, the §10 canon
guard, and graceful fallback on a bad model reply.
"""

from __future__ import annotations

from app.agents.adapter import Adapter
from app.agents.comprehension import BeatComprehension, merge_comprehension
from app.agents.contracts import (
    Beat,
    DialogueLine,
    DiscourseMode,
    LiteraryDevice,
    NarrativePerson,
    SceneTempo,
)
from app.providers import Providers
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)


def _floor() -> Beat:
    return Beat(
        beat_id="b0",
        summary="She crossed the courtyard.",
        pov=NarrativePerson.THIRD_OMNISCIENT,
        discourse=DiscourseMode.NARRATION,
        tempo=SceneTempo.SCENE,
    )


def test_merge_overwrites_parseable_scalars() -> None:
    refined = BeatComprehension(
        pov="third_limited", discourse="free_indirect", tempo="pause", unreliable=True
    )
    out = merge_comprehension(_floor(), refined)
    assert out.pov is NarrativePerson.THIRD_LIMITED
    assert out.discourse is DiscourseMode.FREE_INDIRECT
    assert out.tempo is SceneTempo.PAUSE
    assert out.unreliable is True


def test_merge_keeps_heuristic_on_unknown_enum() -> None:
    refined = BeatComprehension(pov="omniscient_god_view", tempo="warp_speed")
    out = merge_comprehension(_floor(), refined)
    # Unparseable enum values fall back to the heuristic floor.
    assert out.pov is NarrativePerson.THIRD_OMNISCIENT
    assert out.tempo is SceneTempo.SCENE


def test_merge_canon_guards_pov_character() -> None:
    refined = BeatComprehension(pov="third_limited", pov_character="Mordred")
    out = merge_comprehension(_floor(), refined, known_entities={"Elsa"})
    # Mordred is not in canon → dropped, never invented.
    assert out.pov_character is None
    out2 = merge_comprehension(_floor(), refined, known_entities={"Mordred"})
    assert out2.pov_character == "Mordred"


def test_merge_canon_guards_dialogue_speaker() -> None:
    refined = BeatComprehension(
        dialogue=[DialogueLine(speaker="Ghost", quote="Beware", inferred=False)]
    )
    out = merge_comprehension(_floor(), refined, known_entities={"Elsa"})
    assert out.dialogue[0].speaker == ""  # invented speaker dropped


def test_merge_omniscient_clears_focal() -> None:
    floor = _floor().model_copy(update={"pov_character": "Elsa"})
    refined = BeatComprehension(pov="third_omniscient")
    out = merge_comprehension(floor, refined, known_entities={"Elsa"})
    assert out.pov_character is None


def test_merge_adds_devices() -> None:
    refined = BeatComprehension(
        devices=[LiteraryDevice(kind="metaphor", text="x", visual_intent="a sinking weight")]
    )
    out = merge_comprehension(_floor(), refined)
    assert out.devices and out.devices[0].visual_intent == "a sinking weight"


async def test_enrich_beat_llm_merges_canned_reply(providers: Providers) -> None:  # noqa: F811
    reply = {
        "pov": "third_limited",
        "pov_character": "Elsa",
        "discourse": "free_indirect",
        "tempo": "pause",
        "unreliable": False,
        "dialogue": [],
        "devices": [],
    }
    providers.chat.chat_json = JsonSequencer(reply)  # type: ignore[method-assign]
    beat = Beat(beat_id="b0", summary="Elsa watched the snow fall, and how it pleased her.")
    out = await Adapter(providers).enrich_beat_llm(beat, known_entities={"Elsa"})
    assert out.pov is NarrativePerson.THIRD_LIMITED
    assert out.pov_character == "Elsa"
    assert out.discourse is DiscourseMode.FREE_INDIRECT


async def test_enrich_beat_llm_falls_back_on_bad_reply(providers: Providers) -> None:  # noqa: F811
    # A reply that fails validation twice (so the BaseAgent repair also fails)
    # must not crash — the heuristic floor is returned.
    providers.chat.chat_json = JsonSequencer("not json at all")  # type: ignore[method-assign]
    beat = Beat(beat_id="b0", summary='"Run!" she cried as the wind howled like a beast.')
    out = await Adapter(providers).enrich_beat_llm(beat, known_entities={"Elsa"})
    # Heuristic floor still populated the literary fields.
    assert out.tempo is SceneTempo.SCENE
    assert out.dialogue  # the heuristic diarized the quote
