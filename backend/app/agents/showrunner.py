"""Showrunner / Orchestrator — production planning + conflict arbitration (§7, §7.2).

The expensive model (``qwen3.7-max``), called sparingly. Its single-book jobs:

* :meth:`plan_production` — decompose a book summary into a high-level scene plan;
* :meth:`arbitrate` — resolve a :class:`ConflictObject` under the FIXED §7.2
  policy and emit a :class:`DecisionRecord`.

The policy itself is the pure, deterministic :func:`decide_arbitration` — it
takes the conflict, whether the source text supports the change, and whether a
director is present, and returns the chosen option. That separation is what lets
all three branches (evolve / surface / honor) be unit-tested without a network:
the textual-support judgment is injectable.

**Series-scale showrunning (§7).** Beyond one book, the Showrunner orchestrates a
*series*: a cross-book bible, multi-volume character/relationship arcs, pacing
curves, episode/act structure, "previously on" recaps, thematic motifs, and a
richer arbitration that weighs arc continuity and dramatic stakes. As with the
single-book policy, **every decision is a pure function** in
:mod:`app.agents.series`; the model is invoked only to *narrate* the plans those
functions produce (recap prose, a bible synopsis). The methods here are thin
orchestration over that pure layer:

* :meth:`arbitrate` accepts an optional :class:`ArbitrationContext` and, when
  given, threads it through the weighed §7.2 scoring while still returning a
  back-compatible :class:`DecisionRecord` — callers that pass no context get
  byte-for-byte today's behaviour;
* :meth:`plan_series_volume` annotates a scene plan with the optimized pacing
  curve + act assignment and decides whether a re-plan is warranted;
* :meth:`synthesize_recap` / :meth:`synthesize_bible_synopsis` are prose-only
  model calls over a pre-computed plan.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.providers import Providers

from . import series
from .base import BaseAgent
from .contracts import (
    ArbitrationContext,
    ArbitrationDecision,
    ArcBeat,
    ConflictObject,
    ConflictOption,
    DecisionRecord,
    Motif,
    RecapSpec,
    ScenePlan,
    SeriesBible,
    TextualSupport,
    Volume,
)
from .prompts import SERIES, SHOWRUNNER
from .series.planner import ReplanDirective


def decide_arbitration(
    conflict: ConflictObject,
    *,
    textual_support: bool,
    director_present: bool,
    context: ArbitrationContext | None = None,
) -> tuple[ConflictOption, bool]:
    """Apply the §7.2 resolution policy. Returns ``(chosen_option, evolved_canon)``.

    Policy (in order):
      1. evolve the canon — only when the conflict offers that option AND the
         source text genuinely supports the change;
      2. surface to the user — when a director is present and the conflict is
         user-facing;
      3. honor the canon — the safe default.

    ``context`` is an additive, backward-compatible series-scale hook: when given,
    the honor-vs-surface tie is deferred to the weighed score
    (:func:`app.agents.series.arbitration.weigh_arbitration`) so a high-stakes,
    user-facing conflict near a climax surfaces instead of silently honouring.
    The §7.2 *invariant* is never violated — evolve still requires textual support,
    and a surface still requires a present director on a user-facing conflict. With
    ``context=None`` the behaviour is identical to the original three-branch gate.
    """
    if context is None:
        offers_evolve = any(opt.id is ConflictOption.EVOLVE_CANON for opt in conflict.options)
        if offers_evolve and textual_support:
            return ConflictOption.EVOLVE_CANON, True
        if director_present and conflict.user_facing:
            return ConflictOption.SURFACE_TO_USER, False
        return ConflictOption.HONOR_CANON, False

    decision = series.weigh_arbitration(
        conflict,
        context,
        textual_support=textual_support,
        director_present=director_present,
    )
    # The §7.2 gate owns evolve/surface eligibility; within the non-evolve region
    # we honour the weighed recommendation (which only ever upgrades honor→surface
    # when a director is present on a user-facing conflict).
    if decision.chosen_option is ConflictOption.EVOLVE_CANON:
        return ConflictOption.EVOLVE_CANON, decision.evolved_canon
    return decision.recommended_option, False


class RecapNarration(BaseModel):
    """The Showrunner's recap prose over a pre-selected :class:`RecapSpec` (§7)."""

    model_config = ConfigDict(extra="ignore")

    narration: str = ""


class BibleSynopsis(BaseModel):
    """The Showrunner's series synopsis over a pre-built bible (§7)."""

    model_config = ConfigDict(extra="ignore")

    synopsis: str = ""


class Showrunner(BaseAgent):
    """Plans the production and arbitrates conflicts under the fixed policy.

    Single-book *and* series-scale: the cross-book decisions are pure functions in
    :mod:`app.agents.series`; this class orchestrates them and adds two prose-only
    model calls (recap narration, bible synopsis) behind the ``SERIES`` prompt.
    """

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="showrunner",
            model=settings.chat_model_max,
            prompt=SHOWRUNNER,
            skills=skills,  # type: ignore[arg-type]
        )

    # -- single-book planning + arbitration (§7, §7.2) ----------------------- #

    async def plan_production(
        self, book_summary: str, *, title: str | None = None, page_count: int | None = None
    ) -> ScenePlan:
        """Decompose a book summary into an ordered, high-level scene plan."""
        payload = {
            "task": "plan_production",
            "title": title,
            "page_count": page_count,
            "book_summary": book_summary,
        }
        return await self.run_json(payload, ScenePlan, temperature=0.3)

    async def judge_textual_support(
        self, conflict: ConflictObject, source_span_text: str
    ) -> TextualSupport:
        """Ask the model whether the source text genuinely supports the change (§7.2)."""
        payload = {
            "task": "judge_textual_support",
            "conflict": conflict.model_dump(mode="json"),
            "source_span_text": source_span_text,
        }
        return await self.run_json(payload, TextualSupport, temperature=0.0)

    async def arbitrate(
        self,
        conflict: ConflictObject,
        source_span_text: str,
        *,
        director_present: bool,
        textual_support: TextualSupport | None = None,
        context: ArbitrationContext | None = None,
    ) -> DecisionRecord:
        """Resolve ``conflict`` under the §7.2 policy and return a decision record.

        ``textual_support`` may be injected (so the policy branches are testable
        without a network); when omitted, it is judged by a real model call.

        ``context`` is the additive series-scale hook. When ``None`` (the default —
        and what every existing caller passes), the result is identical to the
        original three-branch decision. When given, the decision is the weighed
        §7.2 outcome and the returned record carries the advisory
        ``recommended_option`` / ``scores`` for the agent-activity feed.
        """
        if textual_support is None:
            textual_support = await self.judge_textual_support(conflict, source_span_text)

        if context is None:
            chosen, evolved = decide_arbitration(
                conflict,
                textual_support=textual_support.supported,
                director_present=director_present,
            )
            return DecisionRecord(
                conflict_id=conflict.conflict_id,
                chosen_option=chosen,
                reasoning=self._reasoning(chosen, textual_support, director_present),
                evolved_canon=evolved,
            )

        weighed = series.weigh_arbitration(
            conflict,
            context,
            textual_support=textual_support.supported,
            director_present=director_present,
        )
        detail = f" ({textual_support.reasoning})" if textual_support.reasoning else ""
        return DecisionRecord(
            conflict_id=conflict.conflict_id,
            chosen_option=weighed.chosen_option,
            reasoning=f"{weighed.reasoning}{detail}",
            evolved_canon=weighed.evolved_canon,
            recommended_option=weighed.recommended_option,
            scores=weighed.scores,
        )

    def weigh_conflict(
        self,
        conflict: ConflictObject,
        context: ArbitrationContext,
        *,
        textual_support: bool,
        director_present: bool,
    ) -> ArbitrationDecision:
        """The pure series-scale arbitration (no model call) — the §7.2 scoring layer.

        Exposed so the live conflict-resolution layer (or the feed) can read the
        full scored decision without a round-trip; :meth:`arbitrate` wraps it into
        the back-compatible :class:`DecisionRecord`.
        """
        return series.weigh_arbitration(
            conflict,
            context,
            textual_support=textual_support,
            director_present=director_present,
        )

    # -- series-scale planning (§7) ------------------------------------------ #

    def plan_series_volume(
        self,
        plan: ScenePlan,
        *,
        scene_tensions: dict[int, float],
        target_acts: int = 3,
        min_pacing_score: float = 0.6,
    ) -> tuple[ScenePlan, ReplanDirective]:
        """Annotate + score a volume's scene plan; flag a re-plan if it drags (§7).

        Pure orchestration over :mod:`app.agents.series.planner`: returns the plan
        annotated with per-scene tension + act and a filled pacing curve, plus a
        :class:`ReplanDirective` bounding the dullest stretch when the pacing score
        falls below ``min_pacing_score`` (the model re-plan round-trip is roadmap
        M7; here we produce the structured directive that would drive it).
        """
        annotated = series.annotate_plan(
            plan, scene_tensions=scene_tensions, target_acts=target_acts
        )
        directive = series.replan_directive(annotated, min_score=min_pacing_score)
        return annotated, directive

    def build_recap(
        self,
        prior_beats: list[ArcBeat],
        *,
        for_volume: int,
        budget_s: float,
        motifs: list[Motif] | None = None,
    ) -> RecapSpec:
        """Select the recap beats under a video-second budget (pure, no model) (§7)."""
        return series.select_recap_beats(
            prior_beats,
            for_volume=for_volume,
            budget_s=budget_s,
            motifs=motifs or [],
        )

    async def replan_for_pacing(
        self,
        plan: ScenePlan,
        *,
        scene_tensions: dict[int, float],
        target_acts: int = 3,
        min_pacing_score: float = 0.6,
    ) -> ScenePlan:
        """The M7 pacing re-plan round-trip (§7): smooth a dragging stretch.

        Annotates + scores the plan; if it paces well, returns it unchanged (no
        model call). Otherwise asks the model to re-plan ONLY the flagged scene
        window under the structured :class:`ReplanDirective` — inject a turn to
        lift tension by the computed deficit — and re-annotates the result so the
        returned plan carries the refreshed pacing curve. The directive (not free
        rein) is what keeps the re-plan grounded; the model never restructures the
        whole volume.
        """
        annotated, directive = self.plan_series_volume(
            plan,
            scene_tensions=scene_tensions,
            target_acts=target_acts,
            min_pacing_score=min_pacing_score,
        )
        if not directive.needed:
            return annotated

        payload = {
            "task": "plan_production",
            "directive": {
                "start_scene": directive.start_scene,
                "end_scene": directive.end_scene,
                "deficit": directive.deficit,
                "note": directive.note,
            },
            "scenes": [s.model_dump(mode="json") for s in annotated.scenes],
        }
        revised = await self.run_json(payload, ScenePlan, temperature=0.3)
        # Keep the series identity and re-annotate against the original tensions so
        # the curve reflects the new ordering; the model supplies the prose only.
        revised = revised.model_copy(
            update={"series_id": plan.series_id, "volume_index": plan.volume_index}
        )
        reannotated, _ = self.plan_series_volume(
            revised,
            scene_tensions=scene_tensions,
            target_acts=target_acts,
            min_pacing_score=min_pacing_score,
        )
        return reannotated

    def assemble(
        self,
        *,
        series_id: str,
        title: str = "",
        volumes: list[Volume],
        volume_arc_beats: dict[int, list[ArcBeat]],
        character_arc_beats: dict[str, list[ArcBeat]] | None = None,
        character_names: dict[str, str] | None = None,
        motifs: list[Motif] | None = None,
        recap_budget_s: float = 12.0,
    ) -> series.SeriesProductionPlan:
        """Compile a whole series into a structured plan (pure, no model) (§7).

        Thin pass-through to :func:`app.agents.series.assemble_series` — the
        deterministic entry point the live system calls once a series' volumes are
        ingested. Recap/synopsis prose is filled afterward by the async methods.
        """
        return series.assemble_series(
            series_id=series_id,
            title=title,
            volumes=volumes,
            volume_arc_beats=volume_arc_beats,
            character_arc_beats=character_arc_beats or {},
            character_names=character_names or {},
            motifs=motifs or [],
            recap_budget_s=recap_budget_s,
        )

    # -- series-scale prose synthesis (model, narration only) (§7) ----------- #

    async def synthesize_recap(self, spec: RecapSpec) -> RecapSpec:
        """Narrate a "previously on" recap over a pre-selected plan (§7).

        The *selection* is already done by :meth:`build_recap` (pure). This asks the
        model only to write the recap paragraph over those exact beats, then returns
        the spec with ``narration`` filled. No new video-seconds are spent — a recap
        reuses accepted clips (§8.7).
        """
        if not spec.items:
            return spec
        payload = series.recap_prompt_payload(spec)
        narration = await self.run_json(
            payload, RecapNarration, temperature=0.4, system=SERIES.system
        )
        return spec.model_copy(update={"narration": narration.narration})

    async def synthesize_bible_synopsis(self, bible: SeriesBible) -> SeriesBible:
        """Narrate a series synopsis over a pre-built bible (§7).

        The arcs/motifs/volumes are computed; the model only writes the through-line
        prose. Returns the bible with ``synopsis`` filled.
        """
        payload = {
            "task": "synthesize_bible",
            "title": bible.title,
            "volumes": [
                {"volume_index": v.volume_index, "title": v.title, "synopsis": v.synopsis}
                for v in bible.volumes
            ],
            "character_arcs": [
                {
                    "name": arc.name or arc.entity_key,
                    "spanned_volumes": arc.spanned_volumes,
                    "final_stage": series.current_arc_state(arc).stage.value,
                }
                for arc in bible.character_arcs
            ],
            "motifs": [{"label": m.label, "description": m.description} for m in bible.motifs],
        }
        synopsis = await self.run_json(
            payload, BibleSynopsis, temperature=0.4, system=SERIES.system
        )
        return bible.model_copy(update={"synopsis": synopsis.synopsis})

    # -- internals ----------------------------------------------------------- #

    @staticmethod
    def _reasoning(
        chosen: ConflictOption, support: TextualSupport, director_present: bool
    ) -> str:
        detail = f" ({support.reasoning})" if support.reasoning else ""
        if chosen is ConflictOption.EVOLVE_CANON:
            return f"Source text supports the change{detail}; evolving canon and regenerating."
        if chosen is ConflictOption.SURFACE_TO_USER:
            return (
                "No textual support for the change and a director is present on a "
                "user-facing conflict; surfacing for the reader to choose."
            )
        director = "no director present" if not director_present else "not user-facing"
        return f"No textual support for the change ({director}); honouring established canon."


__all__ = ["Showrunner", "decide_arbitration"]
