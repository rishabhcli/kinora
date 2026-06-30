"""Domain types for speculative pre-generation (kinora.md §4.4/§4.6, additive).

These are the small, immutable value objects the predictor → planner → engine
pipeline passes around. They are pure data: no behaviour beyond derived
properties, no I/O. ``pydantic`` models carry validation for the inputs that come
from config / callers; frozen dataclasses carry the internal plan artefacts that
the engine builds and never re-validates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field


class PathKind(StrEnum):
    """How the predictor thinks the reader reaches a shot."""

    #: The next contiguous span the reader is steadily advancing into.
    LINEAR = "linear"
    #: A chapter/section/bookmark the reader is statistically likely to jump to.
    JUMP = "jump"
    #: A re-read of an already-visited span (backward glance / cache re-hit).
    REREAD = "reread"


class ModelClass(StrEnum):
    """Coarse cost/quality tier a candidate may be rendered at (§4.6 routing)."""

    #: Cheapest turbo id — what low-probability speculation should use.
    CHEAP = "cheap"
    #: Mid id — moderate-probability speculation.
    STANDARD = "standard"
    #: Premium id — reserved for committed / high-probability shots.
    PREMIUM = "premium"


class ReaderState(BaseModel):
    """A snapshot of where/how the reader is, the predictor's only input.

    ``focus_word`` is the current reading position (word index); ``velocity_wps``
    is the clamped reading velocity in words/second; ``steady`` mirrors the §4.6
    stability gate (an unsteady skimmer's linear forecast is untrustworthy, so the
    predictor down-weights linear paths when this is ``False``).
    """

    model_config = {"frozen": True}

    focus_word: int = Field(ge=0)
    velocity_wps: float = Field(gt=0.0)
    steady: bool = True
    #: Mean dwell per position in ms (long dwell ⇒ a jump is *less* imminent).
    dwell_ms: float = Field(default=200.0, ge=0.0)


class UpcomingShot(BaseModel):
    """A candidate shot the reader has not yet reached (the unit of speculation).

    ``shot_key`` is the cache/canon key (stable across re-reads). ``word_start`` is
    where the shot begins in reading order. ``video_seconds`` is its rendered
    length. ``value`` is the *buffer-hit value* of having it ready — i.e. how much
    a hit on this shot is worth (a shot at a dramatic beat may be worth more than a
    filler transition); defaults to its duration so "value == seconds buffered".
    """

    model_config = {"frozen": True}

    shot_key: str
    word_start: int = Field(ge=0)
    video_seconds: float = Field(gt=0.0)
    value: float = Field(default=0.0, ge=0.0)
    #: Optional anchor: this shot sits at the head of a jump target (chapter/TOC).
    is_jump_target: bool = False

    def effective_value(self) -> float:
        """The hit-value to use (falls back to duration when unset)."""
        return self.value if self.value > 0.0 else self.video_seconds


@dataclass(frozen=True, slots=True)
class PredictedReach:
    """A predicted way the reader reaches ``shot`` — a path with a hit-probability.

    ``hit_probability`` is ``P(reader reaches this shot before it would expire)``,
    in ``[0, 1]``. ``eta_s`` is the predicted reading-seconds until the reader
    arrives (the planner rejects a candidate whose render can't beat its ETA).
    """

    shot: UpcomingShot
    kind: PathKind
    hit_probability: float
    eta_s: float

    @property
    def waste_probability(self) -> float:
        """``1 − hit_probability``: chance the spend is wasted."""
        return max(0.0, 1.0 - self.hit_probability)


@dataclass(frozen=True, slots=True)
class SpeculationChoice:
    """One sized-and-priced speculation: a reach rendered at a chosen model.

    This is what the EV planner scores and selects into a portfolio. It binds a
    :class:`PredictedReach` to a concrete ``model_id`` with its cost/quality/latency
    resolved by the cost model, so the planner is a pure knapsack over these.
    """

    reach: PredictedReach
    model_id: str
    model_class: ModelClass
    cost_usd: float
    quality: float
    render_latency_s: float

    @property
    def shot_key(self) -> str:
        return self.reach.shot.shot_key

    @property
    def expected_value(self) -> float:
        """``P(hit) × value`` — expected buffer-hit value if we render this."""
        return self.reach.hit_probability * self.reach.shot.effective_value()

    @property
    def expected_waste_usd(self) -> float:
        """``P(waste) × cost`` — expected dollars thrown away if the reader misses."""
        return self.reach.waste_probability * self.cost_usd

    @property
    def ev_per_dollar(self) -> float:
        """Expected hit-value per dollar of cost (the greedy ranking key).

        Guards a zero/near-zero cost so a free cache-warm candidate sorts first
        without dividing by zero.
        """
        denom = self.cost_usd if self.cost_usd > 1e-9 else 1e-9
        return self.expected_value / denom

    def feasible_for(self, eta_s: float) -> bool:
        """Whether this can finish before the reader arrives (latency < ETA)."""
        return self.render_latency_s <= max(eta_s, 0.0)


@dataclass(frozen=True, slots=True)
class SpeculationPlan:
    """The portfolio the planner selected to launch *now* (kinora.md §4.4).

    ``selected`` is the chosen set under the budget cap; ``skipped`` records the
    candidates that did not make the cut (with a short reason) for observability.
    """

    selected: list[SpeculationChoice] = field(default_factory=list)
    skipped: list[tuple[SpeculationChoice, str]] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.selected), 6)

    @property
    def total_expected_value(self) -> float:
        return round(sum(c.expected_value for c in self.selected), 6)

    @property
    def total_expected_waste_usd(self) -> float:
        return round(sum(c.expected_waste_usd for c in self.selected), 6)

    @property
    def shot_keys(self) -> list[str]:
        return [c.shot_key for c in self.selected]


@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    """The result of invalidating a speculation when the reader's path changed.

    ``refunded_usd`` is the reservation returned to the speculative budget (only
    for shots not yet *started* — a started render's seconds are sunk). ``salvaged``
    are shot keys whose assets the cache kept for a likely re-hit (§4.8).
    """

    cancelled: list[str] = field(default_factory=list)
    refunded_usd: float = 0.0
    salvaged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


__all__ = [
    "CancellationOutcome",
    "ModelClass",
    "PathKind",
    "PredictedReach",
    "ReaderState",
    "SpeculationChoice",
    "SpeculationPlan",
    "UpcomingShot",
]
