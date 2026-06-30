"""The storyboard planner: passage + canon context → a validated :class:`Storyboard`.

The engine is the orchestration root. It is model-agnostic: the only creative
step is the :class:`ReasoningProvider` call (segment + propose coverage); every
other step is the deterministic decomposition the rest of this package implements.
The output is the canonical shot list the Round-1 prompt-dialect + planner layers
consume — produced, never imported.

Pipeline (per :func:`StoryboardPlanner.plan`):

1. **Plan.** Ask the provider to segment the passage into beats and (optionally)
   propose coverage. Fall back to deterministic segmentation when the passage has
   no beats and the provider returns none.
2. **Cover.** For each beat, take the provider's coverage roles or derive them
   deterministically (:mod:`coverage`); these are the *candidate* roles.
3. **Budget the shot count.** Trim the candidates across beats to fit ``max_shots``
   by tempo density (:mod:`budget`).
4. **Build shots.** Materialise each granted role into a :class:`StoryboardShot`
   with its render mode, entities, narration slice, and source span.
5. **Budget the durations.** Allocate ``target_total_s`` across the shots, clamped
   to the band (:mod:`budget`).
6. **Link continuity.** Thread the §9.3 hand-offs (:mod:`continuity`).
7. **Validate + refine.** Run the validators; if there are errors, run a single
   bounded **re-plan/refine** pass that repairs what it can (relax the shot
   ceiling for an uncovered beat, re-split narration) and re-validates. Residual
   issues are attached as warnings rather than raising.

``plan`` is async because the provider seam is async; the deterministic core is
synchronous and individually testable.
"""

from __future__ import annotations

import structlog

from app.agents.comprehension.text_utils import words as tokenize_words
from app.agents.contracts import SourceSpan

from .budget import (
    BeatAllocation,
    ShotDurationInput,
    allocate_durations,
    allocate_shot_counts,
)
from .continuity import link_continuity
from .coverage import entities_for, plan_coverage, render_mode_for
from .models import (
    CanonContext,
    Passage,
    PassageBeat,
    ShotCoverage,
    ShotIntentShape,
    Storyboard,
    StoryboardBudget,
    StoryboardShot,
    StoryboardWarning,
)
from .provider import BeatPlan, HeuristicReasoningProvider, ReasoningPlan, ReasoningProvider
from .segmentation import segment_passage
from .validators import IssueSeverity, ValidationIssue, has_errors, validate_storyboard

log = structlog.get_logger(__name__)

#: How a coverage role labels the verb of its one-line action brief.
_COVERAGE_ACTION_VERB: dict[ShotCoverage, str] = {
    ShotCoverage.ESTABLISHING: "Establish",
    ShotCoverage.MASTER: "Show",
    ShotCoverage.INSERT: "Cut to a detail of",
    ShotCoverage.REACTION: "React —",
    ShotCoverage.POV: "From the vantage of",
    ShotCoverage.TRANSITION: "Transition past",
}


class StoryboardPlanner:
    """Plans a :class:`Storyboard` for a passage over a pluggable reasoning seam."""

    def __init__(self, provider: ReasoningProvider | None = None) -> None:
        self._provider: ReasoningProvider = provider or HeuristicReasoningProvider()

    async def plan(
        self, passage: Passage, budget: StoryboardBudget | None = None
    ) -> Storyboard:
        """Produce a validated storyboard for ``passage`` fit to ``budget``."""
        budget = budget or StoryboardBudget()
        plan = await self._provider.plan_passage(passage)
        beats = self._beats_from(passage, plan)
        if not beats:
            return Storyboard(
                passage_id=passage.passage_id,
                scene_id=passage.scene_id,
                budget=budget,
                warnings=[
                    StoryboardWarning(code="empty_passage", message="no beats to storyboard")
                ],
            )

        storyboard = self._assemble(passage, beats, plan, budget)

        # Validate + a single bounded refine pass.
        issues = validate_storyboard(
            storyboard, beats, allowed_entities=set(passage.context.entities)
        )
        if has_errors(issues):
            storyboard = self._refine(passage, beats, plan, budget, issues)
            issues = validate_storyboard(
                storyboard, beats, allowed_entities=set(passage.context.entities)
            )

        storyboard.warnings.extend(self._issues_to_warnings(issues))
        return storyboard

    # -- step 1: beats ------------------------------------------------------- #

    def _beats_from(self, passage: Passage, plan: ReasoningPlan) -> list[PassageBeat]:
        """Resolve the beats: prefer the passage's own, then the plan, then segment."""
        if passage.beats:
            return list(passage.beats)
        if plan.beats:
            return [self._beat_from_plan(passage, bp) for bp in plan.beats]
        return segment_passage(passage)

    @staticmethod
    def _beat_from_plan(passage: Passage, bp: BeatPlan) -> PassageBeat:
        # The provider's entities are constrained to the passage's canon context
        # (the no-invent guardrail) — anything it proposes outside the slice is
        # dropped here rather than propagated into a shot.
        allowed = set(passage.context.entities)
        return PassageBeat(
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

    # -- steps 2-6: assemble ------------------------------------------------- #

    def _assemble(
        self,
        passage: Passage,
        beats: list[PassageBeat],
        plan: ReasoningPlan,
        budget: StoryboardBudget,
        *,
        max_shots_override: int | None = None,
    ) -> Storyboard:
        ctx = passage.context
        coverage_by_plan = {bp.beat_id: bp.coverage for bp in plan.beats}

        # Step 2 — candidate coverage per beat (provider's, else deterministic).
        candidates: list[tuple] = []
        for i, beat in enumerate(beats):
            roles = coverage_by_plan.get(beat.beat_id) or plan_coverage(
                beat, ctx, is_first=(i == 0)
            )
            candidates.append((beat.tempo, list(roles)))

        # Step 3 — fit the shot-count budget.
        effective_budget = budget
        if max_shots_override is not None:
            effective_budget = budget.model_copy(update={"max_shots": max_shots_override})
        allocations = allocate_shot_counts(candidates, effective_budget)

        # Step 4 — materialise shots (durations filled in step 5).
        shots: list[StoryboardShot] = []
        duration_inputs: list[ShotDurationInput] = []
        ordinal = 0
        for beat, alloc in zip(beats, allocations, strict=True):
            beat_shots, beat_durinputs, ordinal = self._build_beat_shots(
                beat, alloc, ctx, ordinal
            )
            shots.extend(beat_shots)
            duration_inputs.extend(beat_durinputs)

        # Step 5 — allocate durations across the whole storyboard.
        durations = allocate_durations(duration_inputs, budget)
        shots = [
            s.model_copy(update={"duration_s": d})
            for s, d in zip(shots, durations, strict=True)
        ]

        # Step 6 — continuity hand-offs (may upgrade render modes).
        shots = link_continuity(shots)

        return Storyboard(
            passage_id=passage.passage_id,
            scene_id=passage.scene_id,
            shots=shots,
            budget=budget,
        )

    def _build_beat_shots(
        self,
        beat: PassageBeat,
        alloc: BeatAllocation,
        ctx: CanonContext,
        ordinal: int,
    ) -> tuple[list[StoryboardShot], list[ShotDurationInput], int]:
        """Materialise one beat's granted coverage roles into shots.

        The beat's narration is split evenly across its shots (by word count) so
        the union of the shots' spans covers the beat with no gap — the narration
        coverage invariant the validators enforce.
        """
        roles = alloc.coverage
        if not roles:
            # The shot-count budget granted this beat zero shots (more beats than
            # the ceiling). It is left uncovered here; the validator flags it and
            # the refine pass raises the ceiling so every beat earns a shot.
            return [], [], ordinal
        slices = self._split_narration(beat, len(roles))
        shots: list[StoryboardShot] = []
        durinputs: list[ShotDurationInput] = []
        for idx, (role, (text_slice, span)) in enumerate(zip(roles, slices, strict=True)):
            entities = entities_for(role, beat, ctx)
            refs = [e for e in entities if ctx.is_locked(e)]
            vantage_roles = (ShotCoverage.POV, ShotCoverage.REACTION)
            intent = ShotIntentShape(
                action=self._action_brief(role, beat, entities),
                speakers=entities,
                reference_entities=refs,
                subjective=(role is ShotCoverage.POV) or beat.subjective,
                pov_character=beat.pov_character if role in vantage_roles else None,
                mood=beat.mood,
            )
            shots.append(
                StoryboardShot(
                    shot_id=f"{beat.beat_id}_shot_{idx:02d}",
                    beat_id=beat.beat_id,
                    scene_id=None,
                    ordinal=ordinal,
                    coverage=role,
                    render_mode=render_mode_for(role, beat, ctx),
                    entities=entities,
                    source_span=span,
                    narration=text_slice,
                    intent=intent,
                )
            )
            durinputs.append(
                ShotDurationInput(tempo=beat.tempo, words=max(len(tokenize_words(text_slice)), 1))
            )
            ordinal += 1
        return shots, durinputs, ordinal

    @staticmethod
    def _split_narration(
        beat: PassageBeat, n: int
    ) -> list[tuple[str, SourceSpan]]:
        """Split a beat's narration + word range into ``n`` contiguous slices.

        Splits on word boundaries so each shot carries a real (non-empty) slice
        and the spans tile the beat's word range exactly. With ``n == 1`` the
        whole beat is one slice. A beat with empty text still yields ``n`` slices
        carrying a placeholder so the narration-presence check is satisfied while
        the coverage-gap check (which skips zero-width spans) stays quiet.
        """
        n = max(1, n)
        tokens = beat.text.split()
        lo, hi = beat.word_range
        page = beat.page
        if not tokens:
            # No prose to split — emit placeholders sharing the (possibly zero) span
            # so the narration-presence check passes (coverage-gap skips zero spans).
            placeholder = beat.text.strip() or f"[{beat.beat_id}]"
            return [
                (placeholder, SourceSpan(page=page, word_range=(lo, hi))) for _ in range(n)
            ]
        # Partition token indices into n near-equal contiguous chunks.
        total = len(tokens)
        bounds = [round(k * total / n) for k in range(n + 1)]
        span_total = max(hi - lo, 0)
        slices: list[tuple[str, SourceSpan]] = []
        for k in range(n):
            t0, t1 = bounds[k], bounds[k + 1]
            if t1 <= t0:  # more shots than words — reuse the last token
                t0, t1 = max(0, total - 1), total
            text = " ".join(tokens[t0:t1])
            if span_total:
                w0 = lo + round(t0 * span_total / total)
                w1 = lo + round(t1 * span_total / total)
            else:
                w0 = w1 = lo
            slices.append((text, SourceSpan(page=page, word_range=(w0, w1))))
        return slices

    @staticmethod
    def _action_brief(role: ShotCoverage, beat: PassageBeat, entities: list[str]) -> str:
        verb = _COVERAGE_ACTION_VERB[role]
        who = ", ".join(entities) if entities else "the scene"
        return f"{verb} {who}".strip()

    # -- step 7: refine ------------------------------------------------------ #

    def _refine(
        self,
        passage: Passage,
        beats: list[PassageBeat],
        plan: ReasoningPlan,
        budget: StoryboardBudget,
        issues: list[ValidationIssue],
    ) -> Storyboard:
        """A single bounded repair pass for an invalid first draft.

        The most common fixable defect is a beat left uncovered because the shot
        ceiling was too tight for the number of beats. The refine pass raises the
        effective ``max_shots`` to *at least one shot per beat* (so coverage can
        never starve a beat) and re-assembles. Defects it cannot fix (e.g. an
        infeasible duration band) survive to be reported as warnings.
        """
        codes = {i.code for i in issues}
        max_shots_override: int | None = None
        if {"beat_uncovered", "shot_count_under_minimum"} & codes:
            # Guarantee every beat at least one shot; never shrink below the budget.
            max_shots_override = max(budget.max_shots, len(beats))
            log.info(
                "storyboard.refine.raise_ceiling",
                passage_id=passage.passage_id,
                beats=len(beats),
                new_max_shots=max_shots_override,
            )
        return self._assemble(
            passage, beats, plan, budget, max_shots_override=max_shots_override
        )

    @staticmethod
    def _issues_to_warnings(issues: list[ValidationIssue]) -> list[StoryboardWarning]:
        return [
            StoryboardWarning(
                code=f"unresolved_{i.code}" if i.severity is IssueSeverity.ERROR else i.code,
                message=i.message,
                shot_id=i.shot_id,
            )
            for i in issues
        ]


async def plan_storyboard(
    passage: Passage,
    budget: StoryboardBudget | None = None,
    *,
    provider: ReasoningProvider | None = None,
) -> Storyboard:
    """Convenience: plan a storyboard with a fresh :class:`StoryboardPlanner`."""
    return await StoryboardPlanner(provider).plan(passage, budget)


__all__ = ["StoryboardPlanner", "plan_storyboard"]
