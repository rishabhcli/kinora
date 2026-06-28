"""End-to-end series assembly — compile volumes into a structured plan (§7).

This is the deterministic orchestration that ties the whole series layer together:
given a set of volumes (each with its inferred arc beats), it produces a fully
worked :class:`SeriesProductionPlan` — the cross-book bible, every volume's pacing
curve + act/episode structure, the motif callback schedule, and the recap plans
for each volume after the first. It is the single pure entry point the live system
would call once a series' volumes are ingested; the only model calls (recap /
synopsis prose) happen *after*, on the Showrunner, over this plan.

Pure and composable — every field is built from the lower modules
(:mod:`~app.agents.series.arcs`, `pacing`, `structure`, `motifs`, `recap`,
`bible`, `eval`). No I/O, no model.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import (
    ActBoundary,
    ArcBeat,
    EpisodeBoundary,
    Motif,
    MotifCallback,
    PacingCurve,
    RecapSpec,
    RelationshipKind,
    SeriesBible,
    Volume,
)
from app.agents.series.bible import build_series_bible, volume_beat_counts
from app.agents.series.eval import (
    arc_coherence,
    motif_resolution,
    pacing_quality,
    series_health,
)
from app.agents.series.motifs import plan_all_callbacks
from app.agents.series.pacing import tension_curve
from app.agents.series.recap import select_recap_beats
from app.agents.series.structure import (
    detect_act_boundaries,
    detect_episode_boundaries,
)


class VolumeStructure(BaseModel):
    """The worked structure of a single volume (§7)."""

    model_config = ConfigDict(extra="forbid")

    volume_index: int
    pacing_curve: PacingCurve = Field(default_factory=PacingCurve)
    act_boundaries: list[ActBoundary] = Field(default_factory=list)
    episode_boundaries: list[EpisodeBoundary] = Field(default_factory=list)
    pacing_score: float = 0.0


class SeriesProductionPlan(BaseModel):
    """The fully assembled series plan the live system consumes (§7).

    Carries the bible, each volume's structure, the motif schedule, the per-volume
    recap plans (prose unfilled — the Showrunner narrates them), and a health
    scoreboard for the §13 feed. Everything here is deterministic.
    """

    model_config = ConfigDict(extra="forbid")

    bible: SeriesBible
    volume_structures: list[VolumeStructure] = Field(default_factory=list)
    motif_callbacks: list[MotifCallback] = Field(default_factory=list)
    recaps: list[RecapSpec] = Field(default_factory=list)
    health: dict[str, float] = Field(default_factory=dict)


def assemble_series(
    *,
    series_id: str,
    title: str = "",
    volumes: Sequence[Volume],
    volume_arc_beats: dict[int, list[ArcBeat]],
    character_arc_beats: dict[str, list[ArcBeat]] | None = None,
    character_names: dict[str, str] | None = None,
    relationship_arc_beats: dict[tuple[str, str], list[ArcBeat]] | None = None,
    relationship_kinds: dict[tuple[str, str], RelationshipKind] | None = None,
    motifs: Sequence[Motif] = (),
    target_acts: int = 3,
    target_episodes: int = 3,
    recap_budget_s: float = 12.0,
    echoes_per_volume: int = 1,
) -> SeriesProductionPlan:
    """Compile volumes + arc samples into a full :class:`SeriesProductionPlan` (§7).

    ``volume_arc_beats`` maps a volume index to *all* its arc beats (used for the
    per-volume pacing curve and structure); ``character_arc_beats`` /
    ``relationship_arc_beats`` feed the cross-book arcs in the bible. The recap for
    a volume looks back over every prior volume's beats under ``recap_budget_s``.
    """
    bible = build_series_bible(
        series_id=series_id,
        title=title,
        volumes=volumes,
        character_beats=character_arc_beats or {},
        character_names=character_names or {},
        relationship_beats=relationship_arc_beats or {},
        relationship_kinds=relationship_kinds or {},
        motifs=motifs,
    )

    structures: list[VolumeStructure] = []
    volume_curves: dict[int, PacingCurve] = {}
    for volume in sorted(volumes, key=lambda v: v.volume_index):
        beats = volume_arc_beats.get(volume.volume_index, [])
        curve = tension_curve(beats)
        volume_curves[volume.volume_index] = curve
        structures.append(
            VolumeStructure(
                volume_index=volume.volume_index,
                pacing_curve=curve,
                act_boundaries=detect_act_boundaries(
                    curve, target_acts=target_acts, volume_index=volume.volume_index
                ),
                episode_boundaries=detect_episode_boundaries(
                    curve, target_episodes=target_episodes, volume_index=volume.volume_index
                ),
                pacing_score=pacing_quality(curve).score,
            )
        )

    callbacks = plan_all_callbacks(
        motifs,
        volume_beat_counts=volume_beat_counts(bible),
        echoes_per_volume=echoes_per_volume,
    )

    # A recap for the opening of every volume after the first, looking back over
    # all prior volumes' beats.
    recaps: list[RecapSpec] = []
    all_prior: list[ArcBeat] = []
    for volume in sorted(volumes, key=lambda v: v.volume_index):
        if volume.volume_index > 0 and all_prior:
            recaps.append(
                select_recap_beats(
                    all_prior,
                    for_volume=volume.volume_index,
                    budget_s=recap_budget_s,
                    motifs=list(motifs),
                )
            )
        all_prior = all_prior + volume_arc_beats.get(volume.volume_index, [])

    health = series_health(bible, volume_curves=volume_curves, callbacks=callbacks)

    return SeriesProductionPlan(
        bible=bible,
        volume_structures=structures,
        motif_callbacks=callbacks,
        recaps=recaps,
        health=health,
    )


def plan_summary(plan: SeriesProductionPlan) -> dict[str, object]:
    """A compact, feed-ready summary of an assembled plan (§12.5 observability)."""
    arc = arc_coherence(plan.bible)
    motif = motif_resolution(plan.bible.motifs, plan.motif_callbacks)
    return {
        "series_id": plan.bible.series_id,
        "volumes": len(plan.bible.volumes),
        "tracked_arcs": len(plan.bible.character_arcs) + len(plan.bible.relationship_arcs),
        "arc_coherence": arc.coherence,
        "motif_payoff_rate": motif.payoff_rate,
        "episodes": sum(len(s.episode_boundaries) for s in plan.volume_structures),
        "recaps": len(plan.recaps),
        "overall_health": plan.health.get("overall", 0.0),
    }


__all__ = [
    "SeriesProductionPlan",
    "VolumeStructure",
    "assemble_series",
    "plan_summary",
]
