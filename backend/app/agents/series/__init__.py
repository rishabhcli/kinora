"""Series-scale showrunning — pure policy for multi-volume adaptation (§7, §7.2).

The single-book Showrunner (:mod:`app.agents.showrunner`) plans one volume and
arbitrates one conflict. This package is the *series* layer that lets the same
canon-first architecture survive a multi-volume work — a trilogy or a saga whose
characters, relationships, themes and continuity must hold across books.

Like every other piece of crew *policy* (the §9.3 render-mode tree, the §9.5
Critic routing, the §7.2 arbitration), everything here is **pure, deterministic
and unit-testable** — no I/O, no model calls, no credits. The expensive model
(qwen3.7-max, §11) is invoked only by the Showrunner to *narrate* the plans these
functions produce (recap prose, a bible synopsis).

Modules:

* :mod:`~app.agents.series.arcs` — multi-volume character & relationship arc
  tracking; arc-state evolving over beats; time-travel resolution (§8.5).
* :mod:`~app.agents.series.pacing` — narrative-tension / pacing curves and the
  pacing score the planner optimizes against.
* :mod:`~app.agents.series.structure` — automatic act & episode boundary detection.
* :mod:`~app.agents.series.recap` — budget-bounded "previously on" recap selection.
* :mod:`~app.agents.series.motifs` — thematic-motif planning & callbacks.
* :mod:`~app.agents.series.arbitration` — the richer §7.2 arbitration that weighs
  arc continuity and dramatic stakes.
* :mod:`~app.agents.series.bible` — building & querying the cross-book series bible.
* :mod:`~app.agents.series.continuity` — cross-volume contradiction detection.
* :mod:`~app.agents.series.planner` — pacing-driven scene-plan re-plan signals.
* :mod:`~app.agents.series.eval` — arc/pacing/motif coherence metrics (§13).
"""

from __future__ import annotations

from .arbitration import hard_gate, score_options, weigh_arbitration
from .arcs import (
    advance_arc,
    arc_intensity_trajectory,
    arc_regressions,
    arc_state_at,
    build_character_arc,
    build_relationship_arc,
    current_arc_state,
    fold_arc,
    is_monotonic,
    merge_arc_beats,
    sort_arc_beats,
    stage_progress,
    stage_rank,
)
from .assembly import (
    SeriesProductionPlan,
    VolumeStructure,
    assemble_series,
    plan_summary,
)
from .bible import (
    build_series_bible,
    character_arc,
    character_arc_state_at,
    entities_in_volume,
    merge_into_bible,
    motif_callbacks,
    motifs_due_at,
    relationships_of,
    volume_beat_counts,
    volume_for_beat,
)
from .continuity import (
    PriorFact,
    ProposedFact,
    active_prior_facts,
    detect_cross_volume_conflict,
    scan_cross_volume,
)
from .eval import arc_coherence, motif_resolution, pacing_quality, series_health
from .inference import (
    cue_boost,
    infer_arc_beat,
    infer_arc_from_beats,
    infer_character_arc_across_volumes,
    infer_scene_tensions,
    mood_intensity,
    stage_for_position,
)
from .motifs import (
    due_callbacks,
    motif_is_resolved,
    plan_all_callbacks,
    plan_motif_callbacks,
    unresolved_motifs,
)
from .pacing import (
    beat_tension,
    curve_from_tensions,
    longest_flat_run,
    monotony_fraction,
    pacing_score,
    peak_position,
    rising_fraction,
    smooth_curve,
    tension_curve,
    worst_pacing_window,
)
from .planner import (
    ReplanDirective,
    annotate_plan,
    curve_from_arc_beats,
    plan_pacing_score,
    replan_directive,
    smooth_plan_tensions,
)
from .recap import recap_prompt_payload, recap_weight, select_recap_beats
from .structure import (
    assign_acts_to_beats,
    detect_act_boundaries,
    detect_episode_boundaries,
)

__all__ = [
    "PriorFact",
    "ProposedFact",
    "ReplanDirective",
    "SeriesProductionPlan",
    "VolumeStructure",
    "active_prior_facts",
    "advance_arc",
    "annotate_plan",
    "arc_coherence",
    "arc_intensity_trajectory",
    "arc_regressions",
    "arc_state_at",
    "assemble_series",
    "assign_acts_to_beats",
    "beat_tension",
    "build_character_arc",
    "build_relationship_arc",
    "build_series_bible",
    "character_arc",
    "character_arc_state_at",
    "cue_boost",
    "current_arc_state",
    "curve_from_arc_beats",
    "curve_from_tensions",
    "detect_act_boundaries",
    "detect_cross_volume_conflict",
    "detect_episode_boundaries",
    "due_callbacks",
    "entities_in_volume",
    "fold_arc",
    "hard_gate",
    "infer_arc_beat",
    "infer_arc_from_beats",
    "infer_character_arc_across_volumes",
    "infer_scene_tensions",
    "is_monotonic",
    "longest_flat_run",
    "merge_arc_beats",
    "merge_into_bible",
    "monotony_fraction",
    "mood_intensity",
    "motif_callbacks",
    "motif_is_resolved",
    "motif_resolution",
    "motifs_due_at",
    "pacing_quality",
    "pacing_score",
    "peak_position",
    "plan_all_callbacks",
    "plan_motif_callbacks",
    "plan_pacing_score",
    "plan_summary",
    "recap_prompt_payload",
    "recap_weight",
    "relationships_of",
    "replan_directive",
    "rising_fraction",
    "scan_cross_volume",
    "score_options",
    "select_recap_beats",
    "series_health",
    "smooth_curve",
    "smooth_plan_tensions",
    "sort_arc_beats",
    "stage_for_position",
    "stage_progress",
    "stage_rank",
    "tension_curve",
    "unresolved_motifs",
    "volume_beat_counts",
    "volume_for_beat",
    "weigh_arbitration",
    "worst_pacing_window",
]
