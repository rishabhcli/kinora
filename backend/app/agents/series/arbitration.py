"""Series-scale arbitration: weigh arc continuity & dramatic stakes (§7.2).

The single-book §7.2 policy (:func:`app.agents.showrunner.decide_arbitration`) is
a fixed 3-branch gate: evolve only with textual support, else surface to a present
director on a user-facing conflict, else honor canon. That gate is **authoritative
and unchanged** — it owns the chosen option.

At series scale there is more to weigh. When the hard gate leaves *honor* and
*surface* both eligible (a user-facing conflict, no textual support, a director
present), which better serves a multi-volume story? A contradiction that touches a
character's central arc near a climax has high *dramatic stakes* — surfacing it to
the reader (a real fork in the adaptation) is the better beat. A trivial
contradiction far from any arc peak should just honor the established canon and
move on. This module computes those weights as a **pure scoring layer** that
*recommends* a branch and explains why, then defers to the §7.2 gate.

:func:`weigh_arbitration` returns an
:class:`~app.agents.contracts.ArbitrationDecision` carrying both the gate's
authoritative ``chosen_option`` and the score-based ``recommended_option`` plus the
per-option ``scores`` — exactly the transparency the §7.2 agent-activity feed wants.
"""

from __future__ import annotations

from app.agents.contracts import (
    ArbitrationContext,
    ArbitrationDecision,
    ConflictObject,
    ConflictOption,
)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _offers(conflict: ConflictObject, option: ConflictOption) -> bool:
    return any(opt.id is option for opt in conflict.options)


def hard_gate(
    conflict: ConflictObject,
    *,
    textual_support: bool,
    director_present: bool,
) -> tuple[ConflictOption, bool]:
    """The §7.2 hard gate, identical in spirit to ``decide_arbitration``.

    Kept here as the single source of the *invariant* the scoring layer must never
    violate: evolve only with textual support, else surface when a director is
    present on a user-facing conflict, else honor. Returns ``(option, evolved)``.
    """
    if _offers(conflict, ConflictOption.EVOLVE_CANON) and textual_support:
        return ConflictOption.EVOLVE_CANON, True
    if director_present and conflict.user_facing:
        return ConflictOption.SURFACE_TO_USER, False
    return ConflictOption.HONOR_CANON, False


def score_options(
    conflict: ConflictObject,
    context: ArbitrationContext,
    *,
    textual_support: bool,
) -> dict[ConflictOption, float]:
    """Score each resolution option in ``[0, 1]`` for how well it serves the series.

    Pure and deterministic. The signals (all from :class:`ArbitrationContext`):

    * **evolve** scores high only when there is textual support *and* a motif
      payoff is pending or the conflict spans volumes — i.e. the story genuinely
      moved and the change carries weight;
    * **surface** scores with the dramatic stakes and (especially) being in a
      climax — a high-stakes fork is worth the reader's choice;
    * **honor** scores with arc continuity — when keeping the established arc
      matters more than the novelty, hold the line.
    """
    stakes = _clamp01(context.dramatic_stakes)
    continuity = _clamp01(context.arc_continuity_weight)
    climax_boost = 0.25 if context.in_climax else 0.0
    motif_boost = 0.2 if context.motif_payoff_pending else 0.0
    span_boost = 0.15 if context.spans_volumes else 0.0

    evolve = 0.0
    if _offers(conflict, ConflictOption.EVOLVE_CANON) and textual_support:
        evolve = _clamp01(0.5 + motif_boost + span_boost + 0.3 * stakes)

    surface = _clamp01(0.2 + 0.6 * stakes + climax_boost)
    honor = _clamp01(0.3 + 0.6 * continuity - 0.3 * stakes)

    return {
        ConflictOption.EVOLVE_CANON: round(evolve, 4),
        ConflictOption.SURFACE_TO_USER: round(surface, 4),
        ConflictOption.HONOR_CANON: round(honor, 4),
    }


def weigh_arbitration(
    conflict: ConflictObject,
    context: ArbitrationContext,
    *,
    textual_support: bool,
    director_present: bool,
) -> ArbitrationDecision:
    """Arbitrate with the §7.2 gate + a series-scale scoring layer (§7.2).

    The gate produces the authoritative ``chosen_option``; the scores produce a
    ``recommended_option``. They agree except in the honor-vs-surface region: when
    the gate would *honor* but a director is present and the **surface** score
    beats the **honor** score (high stakes / in a climax), the recommendation
    upgrades to surface — a hint the live system may act on, while the gate's pick
    remains the safe default unless a caller opts to follow the recommendation.
    """
    gate_option, evolved = hard_gate(
        conflict,
        textual_support=textual_support,
        director_present=director_present,
    )
    scores = score_options(conflict, context, textual_support=textual_support)

    recommended = gate_option
    # Only refine within the non-evolve region (the gate's evolve decision is final).
    if gate_option is not ConflictOption.EVOLVE_CANON:
        if (
            director_present
            and conflict.user_facing
            and _offers(conflict, ConflictOption.SURFACE_TO_USER)
            and scores[ConflictOption.SURFACE_TO_USER] > scores[ConflictOption.HONOR_CANON]
        ):
            recommended = ConflictOption.SURFACE_TO_USER
        elif scores[ConflictOption.HONOR_CANON] >= scores[ConflictOption.SURFACE_TO_USER]:
            recommended = ConflictOption.HONOR_CANON

    return ArbitrationDecision(
        conflict_id=conflict.conflict_id,
        chosen_option=gate_option,
        recommended_option=recommended,
        evolved_canon=evolved,
        scores={k.value: v for k, v in scores.items()},
        reasoning=_explain(gate_option, recommended, context, textual_support),
    )


def _explain(
    chosen: ConflictOption,
    recommended: ConflictOption,
    context: ArbitrationContext,
    textual_support: bool,
) -> str:
    """A one-line, feed-ready justification of the weighed decision."""
    if chosen is ConflictOption.EVOLVE_CANON:
        return (
            "Source text supports the change and the series stakes carry it; "
            "evolving canon and regenerating."
        )
    stakes = f"stakes={context.dramatic_stakes:.2f}"
    cont = f"arc_continuity={context.arc_continuity_weight:.2f}"
    if chosen is ConflictOption.SURFACE_TO_USER:
        return f"User-facing conflict at high stakes ({stakes}); surfacing to the director."
    if recommended is ConflictOption.SURFACE_TO_USER:
        return (
            f"Honouring canon by policy, but recommending a surface: {stakes} near a "
            "dramatic high point outweighs continuity."
        )
    return (
        f"No textual support and arc continuity matters more here ({cont}); "
        "honouring established canon."
    )


__all__ = [
    "hard_gate",
    "score_options",
    "weigh_arbitration",
]
