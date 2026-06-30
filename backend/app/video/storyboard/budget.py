"""Shot-count + duration budgeting: fit a storyboard to a pacing target (§4.4, §11).

Two allocations, both deterministic:

1. **Shot-count budget.** Each beat proposes ordered candidate coverage roles
   (:mod:`app.video.storyboard.coverage`, richest first). The total candidates
   across a passage usually exceed the §-style ``max_shots`` ceiling — the scarce
   video-seconds are bounded by reading. :func:`allocate_shot_counts` distributes
   the ceiling across beats by **tempo density** (a dramatised SCENE earns denser
   coverage than a compressed SUMMARY), always granting every beat its head shot,
   then trims each beat's candidate list from the tail.

2. **Duration budget.** Once the shot count is fixed, :func:`allocate_durations`
   splits the ``target_total_s`` across the shots — weighted by each shot's tempo
   duration-bias and narration length — then clamps every shot to the wan
   ``[min_shot_s, max_shot_s]`` band and redistributes any clamp residual so the
   realised total lands within ``tolerance_s`` of the target whenever the band
   allows it.

Pure functions over plain dataclasses; no models imported beyond the budget value
object, so they compose cleanly under the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.comprehension.pacing import density_multiplier, duration_bias
from app.agents.contracts import SceneTempo

from .models import ShotCoverage, StoryboardBudget


@dataclass(frozen=True, slots=True)
class BeatAllocation:
    """The shot-count decision for one beat: its trimmed, ordered coverage roles."""

    beat_index: int
    tempo: SceneTempo
    coverage: list[ShotCoverage]


@dataclass(slots=True)
class _BeatDemand:
    """Internal: a beat's candidate roles + its density weight during allocation."""

    beat_index: int
    tempo: SceneTempo
    candidates: list[ShotCoverage]
    granted: int = 1  # every beat keeps its head shot
    weight: float = field(default=1.0)


def allocate_shot_counts(
    candidates_per_beat: list[tuple[SceneTempo, list[ShotCoverage]]],
    budget: StoryboardBudget,
) -> list[BeatAllocation]:
    """Distribute the ``max_shots`` ceiling across beats by tempo density.

    Algorithm (deterministic, stable):

    - Every beat is granted its **head** candidate first (so no beat vanishes),
      capped at ``max_shots`` total — if there are more beats than the ceiling,
      the earliest beats win their head and later beats get zero.
    - Remaining budget is handed out one shot at a time to the beat with the
      highest *unmet demand pressure* = ``density_weight × (candidates_left)`` /
      ``(granted)``, breaking ties by reading order. A denser tempo (higher
      ``density_multiplier``) therefore accrues extra coverage first.
    - A beat never receives more shots than it has candidate roles.

    Returns one :class:`BeatAllocation` per input beat (in order), each with the
    head-first trimmed coverage list.
    """
    demands = [
        _BeatDemand(
            beat_index=i,
            tempo=tempo,
            candidates=list(roles),
            granted=0,
            weight=density_multiplier(tempo),
        )
        for i, (tempo, roles) in enumerate(candidates_per_beat)
    ]
    # Beats with no candidate roles (shouldn't happen — coverage always emits a
    # head) get a synthetic master so the loop is total.
    for d in demands:
        if not d.candidates:
            d.candidates = [ShotCoverage.MASTER]

    ceiling = max(budget.min_shots, min(budget.max_shots, _total_candidates(demands)))
    remaining = ceiling

    # Pass 1 — grant each beat its head, earliest-first, until the ceiling binds.
    for d in demands:
        if remaining <= 0:
            break
        d.granted = 1
        remaining -= 1

    # Pass 2 — distribute the rest by demand pressure.
    while remaining > 0:
        best = _pick_pressured_beat(demands)
        if best is None:
            break
        best.granted += 1
        remaining -= 1

    return [
        BeatAllocation(
            beat_index=d.beat_index,
            tempo=d.tempo,
            coverage=d.candidates[: d.granted],
        )
        for d in demands
    ]


def _total_candidates(demands: list[_BeatDemand]) -> int:
    return sum(len(d.candidates) for d in demands)


def _pick_pressured_beat(demands: list[_BeatDemand]) -> _BeatDemand | None:
    """The beat with the most unmet, density-weighted demand (None if all full)."""
    best: _BeatDemand | None = None
    best_pressure = 0.0
    for d in demands:
        left = len(d.candidates) - d.granted
        if left <= 0 or d.granted <= 0:
            continue
        # Pressure favours denser tempo and beats not yet near their cap.
        pressure = d.weight * left / d.granted
        if pressure > best_pressure + 1e-9:
            best_pressure = pressure
            best = d
    return best


@dataclass(frozen=True, slots=True)
class ShotDurationInput:
    """Per-shot inputs to duration allocation: tempo + narration word count."""

    tempo: SceneTempo
    words: int


def allocate_durations(
    shots: list[ShotDurationInput],
    budget: StoryboardBudget,
) -> list[float]:
    """Split ``target_total_s`` across shots, weighted + clamped to the band.

    Each shot's raw weight is ``duration_bias(tempo) × max(words, 1)`` so a held
    PAUSE and a wordier shot earn more screen-time. Weights are normalised to the
    target, every value is clamped to ``[min_shot_s, max_shot_s]``, and the
    clamp residual (target − Σ clamped) is redistributed proportionally across the
    shots that still have headroom — iterating until the residual is exhausted or
    no shot can absorb more. The result is rounded to 0.1s.

    When the target is infeasible for the band (e.g. ``target < n × min_shot_s``)
    the band wins — every shot sits at its clamp and the realised total may fall
    outside ``tolerance_s`` (the validators flag it; the engine warns).
    """
    n = len(shots)
    if n == 0:
        return []

    weights = [max(duration_bias(s.tempo), 1e-3) * max(s.words, 1) for s in shots]
    total_w = sum(weights)
    raw = [budget.target_total_s * (w / total_w) for w in weights]

    lo, hi = budget.min_shot_s, budget.max_shot_s
    durations = [min(max(r, lo), hi) for r in raw]

    # Redistribute the clamp residual across shots with headroom.
    durations = _absorb_residual(durations, budget)
    return [round(d, 1) for d in durations]


def _absorb_residual(durations: list[float], budget: StoryboardBudget) -> list[float]:
    """Iteratively push the target − Σ residual into shots that have headroom."""
    lo, hi = budget.min_shot_s, budget.max_shot_s
    target = budget.target_total_s
    out = list(durations)
    for _ in range(64):  # bounded; converges well before this
        residual = target - sum(out)
        if abs(residual) < 1e-6:
            break
        if residual > 0:
            idx = [i for i, d in enumerate(out) if d < hi - 1e-9]
            cap = hi
        else:
            idx = [i for i, d in enumerate(out) if d > lo + 1e-9]
            cap = lo
        if not idx:
            break  # band is saturated — infeasible target, band wins
        share = residual / len(idx)
        for i in idx:
            nxt = out[i] + share
            out[i] = min(max(nxt, lo), hi) if cap in (lo, hi) else nxt
    return out


__all__ = [
    "BeatAllocation",
    "ShotDurationInput",
    "allocate_durations",
    "allocate_shot_counts",
]
