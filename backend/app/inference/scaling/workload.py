"""Load profiles for the scaling simulator (kinora.md §4, §12.2, §13).

The discrete-event simulator validates the autoscaler + SLO router under *varied
load*, so it needs a vocabulary of demand shapes. A :class:`LoadProfile` is a pure
function from sim-time → instantaneous arrival rate (req/s); a request priority
mix layered on top tags each arrival as committed vs. speculative (the §4.4 zones)
so the preemption + shedding logic is exercised. An :class:`ArrivalGenerator`
turns a profile into a deterministic stream of timestamped, prioritised arrivals
via a seeded non-homogeneous Poisson process (thinning), so every simulation run
is reproducible.

Profiles provided:

* :class:`ConstantLoad` — a flat rate (the stationary baseline).
* :class:`RampLoad` — a linear ramp from ``start`` to ``end`` over the window (the
  evening reading population growing).
* :class:`DiurnalLoad` — a sinusoidal daily cycle (peak hour + trough), the shape
  Holt-Winters is meant to anticipate.
* :class:`BurstLoad` — a baseline with a sharp Gaussian spike (a content drop / a
  class assignment hitting at once), the shape that tests shedding + preemption.
* :class:`CompositeLoad` — the sum of several profiles (baseline + diurnal + burst).
* :func:`reader_population_load` — derives an arrival rate from a reader count via
  the §4.1 :class:`~app.reliability.capacity.ReadingProfile`, tying the sim back to
  the product's own consumption model.

Everything deterministic given the seed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.reliability.capacity import ReadingProfile

__all__ = [
    "RequestPriority",
    "LoadProfile",
    "ConstantLoad",
    "RampLoad",
    "DiurnalLoad",
    "BurstLoad",
    "CompositeLoad",
    "Arrival",
    "ArrivalGenerator",
    "reader_population_load",
]


class RequestPriority(StrEnum):
    """The §4.4 zone priority an arrival carries (drives preemption + shedding)."""

    #: Committed-zone full video: the buffer is sacred; never shed/preempted.
    COMMITTED = "committed"
    #: Speculative-zone keyframe/prefetch: droppable + preemptible under pressure.
    SPECULATIVE = "speculative"


class LoadProfile(Protocol):
    """A demand shape: sim-time (seconds) → instantaneous arrival rate (req/s)."""

    def rate_at(self, t: float) -> float:
        """The arrival rate at sim-time ``t`` (non-negative req/s)."""
        ...

    def peak_rate(self) -> float:
        """An upper bound on the rate over all ``t`` (for Poisson thinning)."""
        ...


@dataclass(frozen=True, slots=True)
class ConstantLoad:
    """A flat arrival rate for the whole window."""

    rate: float

    def __post_init__(self) -> None:
        if self.rate < 0.0:
            raise ValueError("rate must be non-negative")

    def rate_at(self, t: float) -> float:
        return self.rate

    def peak_rate(self) -> float:
        return self.rate


@dataclass(frozen=True, slots=True)
class RampLoad:
    """A linear ramp from ``start_rate`` to ``end_rate`` over ``[0, duration_s]``."""

    start_rate: float
    end_rate: float
    duration_s: float

    def __post_init__(self) -> None:
        if self.start_rate < 0.0 or self.end_rate < 0.0:
            raise ValueError("rates must be non-negative")
        if self.duration_s <= 0.0:
            raise ValueError("duration_s must be positive")

    def rate_at(self, t: float) -> float:
        if t <= 0.0:
            return self.start_rate
        if t >= self.duration_s:
            return self.end_rate
        frac = t / self.duration_s
        return self.start_rate + frac * (self.end_rate - self.start_rate)

    def peak_rate(self) -> float:
        return max(self.start_rate, self.end_rate)


@dataclass(frozen=True, slots=True)
class DiurnalLoad:
    """A sinusoidal daily cycle: ``mean + amplitude·sin(2π(t/period − phase))``.

    ``period_s`` is one day by default; ``amplitude`` must not exceed ``mean`` so
    the rate stays non-negative. ``phase`` (in periods) shifts the peak.
    """

    mean_rate: float
    amplitude: float
    period_s: float = 86_400.0
    phase: float = 0.25  # peak in the evening by default

    def __post_init__(self) -> None:
        if self.mean_rate < 0.0:
            raise ValueError("mean_rate must be non-negative")
        if not 0.0 <= self.amplitude <= self.mean_rate:
            raise ValueError("require 0 <= amplitude <= mean_rate (rate stays >= 0)")
        if self.period_s <= 0.0:
            raise ValueError("period_s must be positive")

    def rate_at(self, t: float) -> float:
        angle = 2.0 * math.pi * (t / self.period_s - self.phase)
        return max(0.0, self.mean_rate + self.amplitude * math.sin(angle))

    def peak_rate(self) -> float:
        return self.mean_rate + self.amplitude


@dataclass(frozen=True, slots=True)
class BurstLoad:
    """A baseline plus a Gaussian spike of height ``spike`` at ``center_s``."""

    baseline_rate: float
    spike_rate: float
    center_s: float
    width_s: float

    def __post_init__(self) -> None:
        if self.baseline_rate < 0.0 or self.spike_rate < 0.0:
            raise ValueError("rates must be non-negative")
        if self.width_s <= 0.0:
            raise ValueError("width_s must be positive")

    def rate_at(self, t: float) -> float:
        z = (t - self.center_s) / self.width_s
        return self.baseline_rate + self.spike_rate * math.exp(-0.5 * z * z)

    def peak_rate(self) -> float:
        return self.baseline_rate + self.spike_rate


@dataclass(frozen=True, slots=True)
class CompositeLoad:
    """The pointwise sum of several profiles (baseline + diurnal + burst)."""

    profiles: tuple[LoadProfile, ...]

    def rate_at(self, t: float) -> float:
        return sum(p.rate_at(t) for p in self.profiles)

    def peak_rate(self) -> float:
        return sum(p.peak_rate() for p in self.profiles)


@dataclass(frozen=True, slots=True)
class Arrival:
    """One generated request: when it arrives and its zone priority."""

    t: float
    priority: RequestPriority


@dataclass
class ArrivalGenerator:
    """A seeded non-homogeneous Poisson arrival stream over a :class:`LoadProfile`.

    Uses Lewis–Shedler *thinning*: propose candidate arrivals at the profile's peak
    rate, keep each with probability ``rate_at(t)/peak`` — yielding exact NHPP
    arrival times. Each kept arrival is tagged committed/speculative by an
    independent Bernoulli draw at ``committed_fraction``. Deterministic given the
    seed, so a regression pins to an exact arrival sequence.
    """

    profile: LoadProfile
    horizon_s: float
    committed_fraction: float = 0.4
    seed: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        if self.horizon_s <= 0.0:
            raise ValueError("horizon_s must be positive")
        if not 0.0 <= self.committed_fraction <= 1.0:
            raise ValueError("committed_fraction must be in [0, 1]")
        self._rng = random.Random(self.seed)

    def __iter__(self) -> Iterator[Arrival]:
        peak = self.profile.peak_rate()
        if peak <= 0.0:
            return
        t = 0.0
        while True:
            # Inter-arrival of the dominating homogeneous process at the peak rate.
            t += self._rng.expovariate(peak)
            if t >= self.horizon_s:
                return
            # Thinning: accept with prob rate_at(t)/peak.
            if self._rng.random() <= self.profile.rate_at(t) / peak:
                pr = (
                    RequestPriority.COMMITTED
                    if self._rng.random() < self.committed_fraction
                    else RequestPriority.SPECULATIVE
                )
                yield Arrival(t=t, priority=pr)

    def collect(self) -> list[Arrival]:
        """Materialise the full arrival list (the simulator pre-generates them)."""
        return list(self)


def reader_population_load(
    *, readers: int, profile: ReadingProfile | None = None
) -> ConstantLoad:
    """A constant arrival rate derived from a reader population (§4.1).

    Ties the simulator's load back to the product's own consumption model: each
    active reader offers ``shots_per_second × active_fraction`` shot-renders/s, so
    ``N`` readers offer ``N ×`` that. The result is a :class:`ConstantLoad` the DES
    can drive a backend with — closing the loop between §4.1 and the fleet sim.
    """
    if readers < 0:
        raise ValueError("readers must be non-negative")
    prof = profile or ReadingProfile()
    rate = readers * prof.shots_per_second * prof.active_fraction
    return ConstantLoad(rate=rate)


def _seeded_sequence(
    profile: LoadProfile, *, horizon_s: float, seed: int, committed_fraction: float = 0.4
) -> Sequence[Arrival]:
    """Convenience for tests: a materialised, seeded arrival sequence."""
    return ArrivalGenerator(
        profile=profile,
        horizon_s=horizon_s,
        committed_fraction=committed_fraction,
        seed=seed,
    ).collect()
