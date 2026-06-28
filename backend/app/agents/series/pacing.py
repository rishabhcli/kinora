"""Narrative-tension & pacing curves the planner optimizes against (§7).

A volume's *pacing* is the shape of its narrative tension over the reading
position. Good drama rises, turns, peaks late, and resolves; bad drama sits flat
(monotony) or peaks too early and deflates. The Showrunner uses this curve to:

* place act/episode boundaries (:mod:`app.agents.series.structure`);
* score a plan and find the *worst* stretch to re-plan (:mod:`app.agents.series.planner`);
* report pacing quality to the eval harness (:mod:`app.agents.series.eval`).

The curve is a **read model** built from structured signals already present in a
scene plan / arc beats — chiefly each beat's ``intensity`` and arc ``stage``.
Everything is pure: :func:`tension_curve` builds the
:class:`~app.agents.contracts.PacingCurve` (points + derived stats), and the
scoring functions are deterministic functions of that curve.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.contracts import (
    ArcBeat,
    ArcStage,
    MonotonyRun,
    PacingCurve,
    SourceSpan,
    TensionPoint,
)

#: A multiplier per arc stage — tension is the beat intensity shaped by where in
#: the arc it sits, so a "climax" beat reads hotter than a "setup" beat of equal
#: raw intensity. Deterministic, pre-registered (do not tune to flatter a result).
_STAGE_WEIGHT: dict[ArcStage, float] = {
    ArcStage.SETUP: 0.45,
    ArcStage.RISING: 0.70,
    ArcStage.TURN: 0.85,
    ArcStage.CLIMAX: 1.0,
    ArcStage.FALLING: 0.65,
    ArcStage.RESOLUTION: 0.40,
}

#: A run of samples whose tension stays within this band of its mean is "flat".
_MONOTONY_BAND = 0.06
#: A flat run must be at least this many samples long to count as monotony.
_MONOTONY_MIN_LEN = 3


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def beat_tension(beat: ArcBeat) -> float:
    """The narrative tension of a single beat: intensity shaped by its arc stage."""
    return _clamp01(beat.intensity * _STAGE_WEIGHT.get(beat.stage, 0.6))


def _find_monotony_runs(values: Sequence[float]) -> list[MonotonyRun]:
    """Flat stretches: maximal runs staying within ``_MONOTONY_BAND`` of their mean."""
    runs: list[MonotonyRun] = []
    n = len(values)
    i = 0
    while i < n:
        j = i + 1
        while j < n and abs(values[j] - values[i]) <= _MONOTONY_BAND:
            j += 1
        length = j - i
        if length >= _MONOTONY_MIN_LEN:
            window = values[i:j]
            runs.append(
                MonotonyRun(
                    start_index=i,
                    end_index=j - 1,
                    mean_tension=sum(window) / length,
                    length=length,
                )
            )
        i = j
    return runs


def tension_curve(beats: Iterable[ArcBeat]) -> PacingCurve:
    """Build the pacing curve from arc beats (in series order) (§7).

    Produces a :class:`PacingCurve` with one :class:`TensionPoint` per beat plus
    the derived stats consumers read: the peak (climax) sample index, the mean
    charge, and the monotony runs (flat stretches that want a re-plan).
    """
    ordered = sorted(beats, key=lambda b: (b.volume_index, b.beat_index))
    points: list[TensionPoint] = []
    for beat in ordered:
        points.append(
            TensionPoint(
                volume_index=beat.volume_index,
                beat_index=beat.beat_index,
                tension=beat_tension(beat),
                source_span=beat.source_span or SourceSpan(),
            )
        )
    values = [p.tension for p in points]
    peak_index = max(range(len(values)), key=values.__getitem__) if values else 0
    mean = sum(values) / len(values) if values else 0.0
    return PacingCurve(
        points=points,
        peak_index=peak_index,
        mean_tension=mean,
        monotony_runs=_find_monotony_runs(values),
    )


def curve_from_tensions(
    tensions: Sequence[float], *, volume_index: int = 0
) -> PacingCurve:
    """Build a curve directly from a tension series (skips the beat→tension step).

    Handy for the planner and tests that reason over a raw curve shape.
    """
    points = [
        TensionPoint(volume_index=volume_index, beat_index=i, tension=_clamp01(t))
        for i, t in enumerate(tensions)
    ]
    values = [p.tension for p in points]
    peak_index = max(range(len(values)), key=values.__getitem__) if values else 0
    mean = sum(values) / len(values) if values else 0.0
    return PacingCurve(
        points=points,
        peak_index=peak_index,
        mean_tension=mean,
        monotony_runs=_find_monotony_runs(values),
    )


def peak_position(curve: PacingCurve) -> float:
    """Where the climax sits as a fraction of the curve (0 = first, 1 = last)."""
    n = len(curve.points)
    if n <= 1:
        return 0.0
    return curve.peak_index / (n - 1)


def monotony_fraction(curve: PacingCurve) -> float:
    """The fraction of the curve covered by flat (monotonous) runs (§7)."""
    n = len(curve.points)
    if n == 0:
        return 0.0
    covered = sum(run.length for run in curve.monotony_runs)
    return min(1.0, covered / n)


def longest_flat_run(curve: PacingCurve) -> int:
    """The length of the longest monotonous stretch (0 if none)."""
    return max((run.length for run in curve.monotony_runs), default=0)


def rising_fraction(curve: PacingCurve) -> float:
    """Fraction of adjacent steps that rise — a proxy for sustained build (§7)."""
    pts = curve.points
    if len(pts) < 2:
        return 0.0
    rises = sum(1 for a, b in zip(pts, pts[1:], strict=False) if b.tension > a.tension)
    return rises / (len(pts) - 1)


def pacing_score(curve: PacingCurve) -> float:
    """Score a pacing curve in ``[0, 1]`` — higher is better drama (§7).

    A pure, deterministic blend that rewards the classic dramatic shape:

    * **late peak** — the climax should land in the back third (a too-early peak
      that deflates is penalized);
    * **dynamic range** — a curve that uses the full tension band beats a tepid one;
    * **low monotony** — long flat stretches drag and are penalized.

    Used by the planner to compare candidate plans and to target the worst stretch.
    """
    if not curve.points:
        return 0.0
    if len(curve.points) == 1:
        return _clamp01(curve.points[0].tension)

    values = [p.tension for p in curve.points]
    dynamic_range = max(values) - min(values)

    # Late-peak reward: best when the peak is ~75-100% through; a peak in the
    # first third scores low. Triangular around 0.8.
    pos = peak_position(curve)
    late_peak = 1.0 - min(1.0, abs(pos - 0.8) / 0.8)

    monotony_penalty = monotony_fraction(curve)

    score = 0.45 * late_peak + 0.35 * _clamp01(dynamic_range) + 0.20 * (1.0 - monotony_penalty)
    return _clamp01(score)


def worst_pacing_window(
    curve: PacingCurve, *, window: int = 4
) -> tuple[int, int] | None:
    """The lowest-energy contiguous window of the curve — what the planner re-plans.

    Returns the ``(start_index, end_index)`` of the ``window``-sample stretch with
    the lowest mean tension (ties broken toward the earliest), or ``None`` when the
    curve is shorter than ``window``. This is the dull patch a re-plan should
    inject a turn into (:mod:`app.agents.series.planner`).
    """
    n = len(curve.points)
    if n < window or window <= 0:
        return None
    values = [p.tension for p in curve.points]
    best_start = 0
    best_mean = float("inf")
    for start in range(n - window + 1):
        mean = sum(values[start : start + window]) / window
        if mean < best_mean:
            best_mean = mean
            best_start = start
    return (best_start, best_start + window - 1)


def smooth_curve(curve: PacingCurve, *, window: int = 3) -> PacingCurve:
    """A moving-average smoothing of the curve (for boundary detection robustness).

    Returns a *new* curve with smoothed tensions; positions and derived stats are
    recomputed. Pure — does not mutate the input.
    """
    pts = curve.points
    n = len(pts)
    if n == 0 or window <= 1:
        return curve
    half = window // 2
    smoothed: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        block = [pts[k].tension for k in range(lo, hi)]
        smoothed.append(sum(block) / len(block))
    rebuilt = [
        TensionPoint(
            volume_index=pts[i].volume_index,
            beat_index=pts[i].beat_index,
            tension=smoothed[i],
            source_span=pts[i].source_span,
        )
        for i in range(n)
    ]
    peak_index = max(range(n), key=smoothed.__getitem__)
    return PacingCurve(
        points=rebuilt,
        peak_index=peak_index,
        mean_tension=sum(smoothed) / n,
        monotony_runs=_find_monotony_runs(smoothed),
    )


__all__ = [
    "beat_tension",
    "curve_from_tensions",
    "longest_flat_run",
    "monotony_fraction",
    "pacing_score",
    "peak_position",
    "rising_fraction",
    "smooth_curve",
    "tension_curve",
    "worst_pacing_window",
]
