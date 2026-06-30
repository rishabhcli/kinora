"""End-to-end storyboard engine tests over a FAKE scripted provider (no network).

A :class:`ScriptedReasoningProvider` returns a fixed beat plan per passage, so the
engine's deterministic decomposition (coverage → budget → shots → continuity →
validate → refine) is exercised with a fully controlled creative output and zero
network. ``KINORA_LIVE_VIDEO`` is irrelevant here — nothing renders.
"""

from __future__ import annotations

from app.agents.contracts import RenderMode
from app.video.storyboard import (
    BeatPlan,
    CanonContext,
    ContinuityKind,
    HeuristicReasoningProvider,
    Passage,
    PassageBeat,
    ReasoningPlan,
    ScriptedReasoningProvider,
    ShotCoverage,
    StoryboardBudget,
    StoryboardPlanner,
    has_errors,
    plan_storyboard,
    validate_storyboard,
)


def _scripted_passage() -> tuple[Passage, ScriptedReasoningProvider]:
    passage = Passage(
        passage_id="pX",
        scene_id="scene_007",
        context=CanonContext(
            entities=["mara", "tomas", "hall"],
            locked_entities=["mara", "tomas"],
            location="hall",
            style_tokens=["noir"],
        ),
        word_offset=500,
        page=9,
    )
    plan = ReasoningPlan(
        passage_id="pX",
        beats=[
            # An establishing beat: no character, opens the location.
            BeatPlan(
                beat_id="pX_b0",
                text="The great hall lay cold and empty under a dust-grey light.",
                word_range=(500, 512),
                page=9,
                entities=[],
            ),
            # A dramatised dialogue beat between two locked characters.
            BeatPlan(
                beat_id="pX_b1",
                text='"You came," said Mara. "I had to," answered Tomas, stepping closer.',
                word_range=(512, 526),
                page=9,
                entities=["mara", "tomas"],
            ),
        ],
    )
    return passage, ScriptedReasoningProvider({"pX": plan})


async def test_engine_produces_validated_storyboard() -> None:
    passage, provider = _scripted_passage()
    sb = await StoryboardPlanner(provider).plan(
        passage, StoryboardBudget(target_total_s=24.0, max_shots=8)
    )
    assert sb.passage_id == "pX"
    assert sb.scene_id == "scene_007"
    assert sb.shot_count >= 2
    # The provider was actually consulted.
    assert provider.calls == ["pX"]
    # Self-consistent: re-validating the output yields no errors.
    beats = _beats_from_plan(passage, plan_for(provider, "pX"))
    issues = validate_storyboard(
        sb, beats, allowed_entities=set(passage.context.entities)
    )
    assert not has_errors(issues), [i.code for i in issues]
    assert not sb.warnings


def plan_for(provider: ScriptedReasoningProvider, passage_id: str) -> ReasoningPlan:
    """Read back the scripted plan a provider will return for a passage."""
    return provider._plans[passage_id]  # noqa: SLF001 - test introspection of a fake


def _beats_from_plan(passage: Passage, plan: ReasoningPlan) -> list[PassageBeat]:
    """Reconstruct the engine's beats from a scripted plan (no-invent filtering)."""
    allowed = set(passage.context.entities)
    return [
        PassageBeat(
            beat_id=bp.beat_id,
            text=bp.text,
            word_range=bp.word_range,
            page=bp.page or passage.page,
            entities=[e for e in bp.entities if e in allowed],
            tempo=bp.tempo,
            mood=bp.mood,
            subjective=bp.subjective,
            pov_character=bp.pov_character,
        )
        for bp in plan.beats
    ]


async def test_first_shot_is_establishing_t2v() -> None:
    passage, provider = _scripted_passage()
    sb = await StoryboardPlanner(provider).plan(passage, StoryboardBudget(target_total_s=24.0))
    head = sb.shots[0]
    assert head.coverage is ShotCoverage.ESTABLISHING
    assert head.render_mode is RenderMode.TEXT_TO_VIDEO
    assert head.entities == []
    assert head.continuity.kind is ContinuityKind.SCENE_START


async def test_dialogue_beat_locks_characters_and_covers_reaction() -> None:
    passage, provider = _scripted_passage()
    sb = await StoryboardPlanner(provider).plan(passage, StoryboardBudget(target_total_s=30.0))
    dlg = [s for s in sb.shots if s.beat_id == "pX_b1"]
    assert dlg, "the dialogue beat earned no shots"
    # A reaction was covered (two speakers) and characters are reference-locked.
    coverages = {s.coverage for s in dlg}
    assert ShotCoverage.REACTION in coverages
    masters = [s for s in dlg if s.coverage is ShotCoverage.MASTER]
    assert masters and masters[0].render_mode is RenderMode.REFERENCE_TO_VIDEO
    assert "mara" in masters[0].intent.reference_entities


async def test_budget_is_fit_within_tolerance_when_feasible() -> None:
    passage, provider = _scripted_passage()
    budget = StoryboardBudget(target_total_s=24.0, tolerance_s=2.0, max_shots=8)
    sb = await StoryboardPlanner(provider).plan(passage, budget)
    assert abs(sb.total_duration_s - 24.0) <= 2.0


async def test_narration_tiles_each_beat_with_no_gap() -> None:
    passage, provider = _scripted_passage()
    sb = await StoryboardPlanner(provider).plan(passage, StoryboardBudget(target_total_s=24.0))
    # For each beat, the shots' spans should tile its range contiguously.
    by_beat: dict[str, list[tuple[int, int]]] = {}
    for s in sb.shots:
        by_beat.setdefault(s.beat_id, []).append(s.source_span.word_range)
    for spans in by_beat.values():
        spans.sort()
        for a, b in zip(spans, spans[1:], strict=False):
            assert a[1] == b[0]  # no gap, no overlap
    assert all(s.narration.strip() for s in sb.shots)


async def test_entities_never_escape_canon_context() -> None:
    # A scripted beat that names an entity OUTSIDE the canon slice: the engine
    # drops it (the no-invent guardrail) rather than emitting an orphan shot.
    passage = Passage(
        passage_id="pY",
        context=CanonContext(entities=["mara"], locked_entities=["mara"]),
    )
    plan = ReasoningPlan(
        passage_id="pY",
        beats=[
            BeatPlan(
                beat_id="pY_b0",
                text="Mara faced the dragon alone.",
                word_range=(0, 5),
                entities=["mara", "dragon"],  # dragon is not in canon
            )
        ],
    )
    sb = await StoryboardPlanner(ScriptedReasoningProvider({"pY": plan})).plan(
        passage, StoryboardBudget(target_total_s=5.0)
    )
    for shot in sb.shots:
        assert "dragon" not in shot.entities
        assert "dragon" not in shot.intent.reference_entities
    assert not sb.warnings  # no orphan-entity error survived


async def test_refine_guarantees_every_beat_a_shot() -> None:
    # Five beats but a ceiling of 2 — the first draft starves three beats; the
    # refine pass raises the effective ceiling so every beat earns a shot.
    passage = Passage(
        passage_id="pZ",
        context=CanonContext(entities=["mara"], locked_entities=["mara"]),
    )
    plan = ReasoningPlan(
        passage_id="pZ",
        beats=[
            BeatPlan(
                beat_id=f"pZ_b{i}",
                text=f"Mara moved through room number {i} of the dim corridor.",
                word_range=(i * 10, i * 10 + 10),
                entities=["mara"],
            )
            for i in range(5)
        ],
    )
    sb = await StoryboardPlanner(ScriptedReasoningProvider({"pZ": plan})).plan(
        passage, StoryboardBudget(target_total_s=30.0, max_shots=2)
    )
    covered = {s.beat_id for s in sb.shots}
    assert covered == {f"pZ_b{i}" for i in range(5)}
    # No unresolved coverage error survived the refine pass.
    assert not any(w.code.startswith("unresolved_beat") for w in sb.warnings)


async def test_empty_passage_yields_empty_storyboard_with_warning() -> None:
    passage = Passage(passage_id="pEmpty", text="")
    sb = await plan_storyboard(passage)
    assert sb.shot_count == 0
    assert any(w.code == "empty_passage" for w in sb.warnings)


async def test_heuristic_provider_default_runs_without_a_seam() -> None:
    # No provider supplied → the deterministic HeuristicReasoningProvider drives.
    passage = Passage(
        passage_id="pH",
        text=(
            "The lamp guttered low. Mara crossed the floor. "
            "She paused at the shuttered window, listening to the wind outside."
        ),
        context=CanonContext(entities=["mara"], locked_entities=["mara"]),
    )
    sb = await plan_storyboard(passage, StoryboardBudget(target_total_s=18.0, max_shots=6))
    assert sb.shot_count >= 1
    assert isinstance(StoryboardPlanner()._provider, HeuristicReasoningProvider)  # noqa: SLF001


async def test_determinism_same_input_same_output() -> None:
    passage, provider = _scripted_passage()
    budget = StoryboardBudget(target_total_s=24.0, max_shots=8)
    plan = plan_for(provider, "pX")
    a = await StoryboardPlanner(ScriptedReasoningProvider({"pX": plan})).plan(passage, budget)
    b = await StoryboardPlanner(ScriptedReasoningProvider({"pX": plan})).plan(passage, budget)
    assert a.model_dump() == b.model_dump()
