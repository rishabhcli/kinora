"""Pacing-driven re-plan optimization for the scene plan (§7).

The Showrunner's ``plan_production`` produces a :class:`ScenePlan`. This module
turns the pacing curve (:mod:`app.agents.series.pacing`) into *planning signals*:
it annotates a plan with per-scene tension + act assignment, scores the plan, and
identifies the dull stretch (``worst_pacing_window``) that a re-plan should fix.

The optimization itself stays deterministic and conservative: it does not invent
scenes or rewrite prose (that is the model's job on a real re-plan round-trip,
roadmap M7). It computes *where* and *how much* the curve needs to change — the
structured directive the Showrunner hands back to the model — and provides a pure
``smooth_plan_tensions`` that demonstrates the target shape for tests/eval.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.contracts import (
    ArcBeat,
    PacingCurve,
    ScenePlan,
    ScenePlanItem,
)
from app.agents.series.pacing import (
    beat_tension,
    curve_from_tensions,
    pacing_score,
    worst_pacing_window,
)
from app.agents.series.structure import assign_acts_to_beats, detect_act_boundaries


@dataclass(frozen=True, slots=True)
class ReplanDirective:
    """A structured instruction for the model's re-plan round-trip (§7, roadmap M7).

    ``start_scene``/``end_scene`` bound the dull stretch; ``current_score`` is the
    plan's pacing score; ``deficit`` is how far the worst window's tension sits
    below the plan mean (the lift the re-plan should inject). Empty (``needed`` =
    False) when the plan already paces well.
    """

    needed: bool
    start_scene: int = 0
    end_scene: int = 0
    current_score: float = 0.0
    deficit: float = 0.0
    note: str = ""


def annotate_plan(
    plan: ScenePlan,
    *,
    scene_tensions: dict[int, float],
    target_acts: int = 3,
) -> ScenePlan:
    """Annotate a scene plan with per-scene tension + act assignment (§7).

    ``scene_tensions`` maps a ``scene_index`` to its planned tension (0..1). The
    function builds a pacing curve over the scenes, detects act boundaries, and
    returns a *new* plan whose items carry ``tension``/``act`` and whose
    ``pacing_curve`` is filled. Pure — does not mutate ``plan``.
    """
    ordered = sorted(plan.scenes, key=lambda s: s.scene_index)
    tensions = [scene_tensions.get(s.scene_index, 0.0) for s in ordered]
    curve = curve_from_tensions(tensions, volume_index=plan.volume_index)

    boundaries = detect_act_boundaries(
        curve, target_acts=target_acts, volume_index=plan.volume_index
    )
    # The curve indexes by position; map positions back to scene indices for acts.
    pos_to_scene = {i: ordered[i].scene_index for i in range(len(ordered))}
    # Re-key act boundaries (which use the curve's beat_index == position) to scenes.
    act_assignment_by_pos = assign_acts_to_beats(
        boundaries, beat_indices=list(range(len(ordered)))
    )

    new_scenes: list[ScenePlanItem] = []
    for i, scene in enumerate(ordered):
        new_scenes.append(
            scene.model_copy(
                update={
                    "tension": round(tensions[i], 4),
                    "act": act_assignment_by_pos.get(i, 1),
                    "volume_index": plan.volume_index,
                }
            )
        )
    _ = pos_to_scene  # documented mapping; acts already keyed by position
    return plan.model_copy(update={"scenes": new_scenes, "pacing_curve": curve})


def plan_pacing_score(plan: ScenePlan) -> float:
    """The pacing score of an annotated plan (0 if it has no curve)."""
    if plan.pacing_curve is None:
        return 0.0
    return pacing_score(plan.pacing_curve)


def replan_directive(
    plan: ScenePlan, *, min_score: float = 0.6, window: int = 4
) -> ReplanDirective:
    """Decide whether (and where) a plan needs a pacing re-plan (§7).

    If the plan's pacing score is below ``min_score``, locate the lowest-energy
    window and return a directive bounding the scenes to lift. Otherwise the
    directive is ``needed=False``.
    """
    curve = plan.pacing_curve
    if curve is None or not curve.points:
        return ReplanDirective(needed=False, note="no curve to score")

    score = pacing_score(curve)
    if score >= min_score:
        return ReplanDirective(needed=False, current_score=round(score, 4), note="paces well")

    win = worst_pacing_window(curve, window=window)
    if win is None:
        return ReplanDirective(
            needed=False, current_score=round(score, 4), note="curve too short to window"
        )
    start_pos, end_pos = win
    ordered = sorted(plan.scenes, key=lambda s: s.scene_index)
    start_scene = ordered[start_pos].scene_index if start_pos < len(ordered) else start_pos
    end_scene = ordered[end_pos].scene_index if end_pos < len(ordered) else end_pos
    window_mean = sum(curve.points[i].tension for i in range(start_pos, end_pos + 1)) / (
        end_pos - start_pos + 1
    )
    deficit = max(0.0, curve.mean_tension - window_mean)
    return ReplanDirective(
        needed=True,
        start_scene=start_scene,
        end_scene=end_scene,
        current_score=round(score, 4),
        deficit=round(deficit, 4),
        note=(
            f"flat stretch (scenes {start_scene}-{end_scene}); inject a turn to lift "
            f"tension by ~{deficit:.2f}"
        ),
    )


def smooth_plan_tensions(
    tensions: list[float], window: tuple[int, int], *, lift: float
) -> list[float]:
    """Demonstrate the target shape: lift a dull window toward a mid-curve peak.

    Pure helper used by tests/eval to show a re-plan *would* raise the score. It
    raises the windowed samples by ``lift`` (clamped to 1.0), peaking in the middle
    of the window so the stretch gets a turn instead of staying flat.
    """
    start, end = window
    out = list(tensions)
    span = max(1, end - start)
    for i in range(start, min(end + 1, len(out))):
        # Triangular bump centred on the window's middle.
        frac = 1.0 - abs((i - start) / span - 0.5) * 2.0
        out[i] = min(1.0, out[i] + lift * frac)
    return out


def curve_from_arc_beats(beats: list[ArcBeat]) -> PacingCurve:
    """Convenience: build a curve straight from arc beats for planning (§7)."""
    return curve_from_tensions(
        [beat_tension(b) for b in sorted(beats, key=lambda b: (b.volume_index, b.beat_index))]
    )


__all__ = [
    "ReplanDirective",
    "annotate_plan",
    "curve_from_arc_beats",
    "plan_pacing_score",
    "replan_directive",
    "smooth_plan_tensions",
]
