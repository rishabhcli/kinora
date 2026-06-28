"""Series-coherence eval metrics for the §13 harness (§7, §13).

The §13 eval harness measures the system against pre-registered metrics. The
series layer adds three coherence checks, all pure functions of the bible / curve:

* **arc coherence** — does every tracked arc advance monotonically across volumes
  (no character whose arc-stage regresses)?
* **pacing quality** — is each volume's curve well-shaped (late peak, dynamic
  range, low monotony)?
* **motif resolution** — does every planted motif eventually pay off?

Returning structured reports (not bare floats) keeps the harness output
inspectable, the same way the Critic's :class:`QARecord` is.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.contracts import (
    ArcCoherenceReport,
    Motif,
    MotifCallback,
    MotifReport,
    PacingCurve,
    PacingReport,
    SeriesBible,
)
from app.agents.series.arcs import arc_regressions
from app.agents.series.motifs import motif_is_resolved
from app.agents.series.pacing import (
    longest_flat_run,
    monotony_fraction,
    pacing_score,
    peak_position,
)


def arc_coherence(bible: SeriesBible) -> ArcCoherenceReport:
    """Score arc coherence: the fraction of arcs that never regress (§7, §13)."""
    arcs = list(bible.character_arcs) + list(bible.relationship_arcs)
    checked = len(arcs)
    if checked == 0:
        return ArcCoherenceReport(arcs_checked=0, monotonic_arcs=0, coherence=1.0)

    regressions: list[str] = []
    monotonic = 0
    for arc in arcs:
        bad = arc_regressions(arc)
        if bad:
            label = getattr(arc, "entity_key", None) or "/".join(
                getattr(arc, "entity_keys", ())
            )
            regressions.append(str(label))
        else:
            monotonic += 1
    return ArcCoherenceReport(
        arcs_checked=checked,
        monotonic_arcs=monotonic,
        regressions=regressions,
        coherence=round(monotonic / checked, 4),
    )


def pacing_quality(curve: PacingCurve) -> PacingReport:
    """Report a volume's pacing quality from its curve (§7, §13)."""
    return PacingReport(
        score=round(pacing_score(curve), 4),
        mean_tension=round(curve.mean_tension, 4),
        peak_position=round(peak_position(curve), 4),
        monotony_fraction=round(monotony_fraction(curve), 4),
        longest_flat_run=longest_flat_run(curve),
    )


def motif_resolution(
    motifs: Sequence[Motif], callbacks: Iterable[MotifCallback]
) -> MotifReport:
    """Report motif payoff: how many planted motifs eventually pay off (§7, §13)."""
    cb_list = list(callbacks)
    checked = len(motifs)
    if checked == 0:
        return MotifReport(motifs_checked=0, paid_off=0, payoff_rate=1.0)
    unresolved: list[str] = []
    paid = 0
    for motif in motifs:
        if motif_is_resolved(motif, cb_list):
            paid += 1
        else:
            unresolved.append(motif.motif_id)
    return MotifReport(
        motifs_checked=checked,
        paid_off=paid,
        unresolved=unresolved,
        payoff_rate=round(paid / checked, 4),
    )


def series_health(
    bible: SeriesBible,
    *,
    volume_curves: dict[int, PacingCurve],
    callbacks: Iterable[MotifCallback],
) -> dict[str, float]:
    """A flat scoreboard blending the three coherence checks (§13 dashboard).

    Returns a small dict suitable for logging / the agent-activity feed: arc
    coherence, mean pacing score across volumes, and the motif payoff rate.
    """
    arc = arc_coherence(bible)
    motif = motif_resolution(bible.motifs, callbacks)
    pacing_scores = [pacing_quality(c).score for c in volume_curves.values()]
    mean_pacing = sum(pacing_scores) / len(pacing_scores) if pacing_scores else 0.0
    return {
        "arc_coherence": arc.coherence,
        "mean_pacing": round(mean_pacing, 4),
        "motif_payoff_rate": motif.payoff_rate,
        "overall": round((arc.coherence + mean_pacing + motif.payoff_rate) / 3.0, 4),
    }


__all__ = [
    "arc_coherence",
    "motif_resolution",
    "pacing_quality",
    "series_health",
]
