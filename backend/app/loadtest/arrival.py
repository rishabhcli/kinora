"""Arrival processes — the *open-loop* offered-load schedule (kinora.md §12.2).

Open-loop load means requests arrive from an unbounded population at a target
rate that is **independent of how fast the server responds** — the only model
that exposes backpressure and queueing collapse, because a slow server cannot
slow the arrivals. This module turns a rate envelope into a concrete, ordered
list of *intended send times* (seconds from run start). The generator later sends
each request at (or after) its intended time; recording latency against the
*intended* time — not the actual send time — is what removes coordinated-omission
bias (see :mod:`app.loadtest.collector`).

Four envelopes, all pure schedule math over an injected seeded RNG so the test
suite can pin arrival counts and inter-arrival statistics against the seed:

* **constant** — a fixed rate ``λ`` for the whole run.
* **ramp** — rate climbs linearly from ``start_rate`` to ``end_rate`` (warm-up).
* **spike** — a baseline rate with a rectangular burst to ``peak_rate`` over a
  window (the §4.6 "reader skims/flips wildly" / promotion-storm shape).
* **poisson** — a Poisson process whose *instantaneous* rate follows any of the
  above shapes; inter-arrival gaps are exponential, so arrivals are bursty and
  realistic rather than perfectly evenly spaced.

The deterministic (non-Poisson) shapes place arrivals at the exact instants that
make the local rate equal the target — i.e. arrival ``k`` is at the time where
the cumulative expected count reaches ``k``. That inversion is closed-form for
constant and ramp and piecewise for spike.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class ArrivalShape(StrEnum):
    """The shape of the offered-rate envelope ``λ(t)`` over the run."""

    CONSTANT = "constant"
    RAMP = "ramp"
    SPIKE = "spike"


@dataclass(frozen=True, slots=True)
class RateEnvelope:
    """A piecewise description of the offered rate ``λ(t)`` over ``[0, duration]``.

    All rates are requests/second. The envelope is evaluated by :meth:`rate_at`
    and integrated by :meth:`expected_count` (the closed-form mean number of
    arrivals in ``[0, t]``), which both the deterministic placer and the Poisson
    sampler use. ``duration_s`` is the run length the schedule spans.
    """

    shape: ArrivalShape
    duration_s: float
    base_rate: float = 1.0
    #: RAMP only: the rate at t=0 (overrides ``base_rate`` as the start point).
    start_rate: float | None = None
    #: RAMP only: the rate at t=duration.
    end_rate: float | None = None
    #: SPIKE only: peak rate during the burst window.
    peak_rate: float | None = None
    #: SPIKE only: burst window [spike_start_s, spike_end_s].
    spike_start_s: float = 0.0
    spike_end_s: float = 0.0

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self.base_rate < 0:
            raise ValueError("base_rate must be non-negative")
        if self.shape is ArrivalShape.SPIKE:
            if self.peak_rate is None:
                raise ValueError("SPIKE requires peak_rate")
            if not (0.0 <= self.spike_start_s <= self.spike_end_s <= self.duration_s):
                raise ValueError("SPIKE window must satisfy 0 <= start <= end <= duration")

    def rate_at(self, t: float) -> float:
        """Instantaneous offered rate at time ``t`` (clamped to the run window)."""
        t = max(0.0, min(t, self.duration_s))
        if self.shape is ArrivalShape.CONSTANT:
            return self.base_rate
        if self.shape is ArrivalShape.RAMP:
            r0 = self.start_rate if self.start_rate is not None else self.base_rate
            r1 = self.end_rate if self.end_rate is not None else self.base_rate
            frac = t / self.duration_s
            return r0 + (r1 - r0) * frac
        # SPIKE
        peak = self.peak_rate or self.base_rate
        if self.spike_start_s <= t <= self.spike_end_s:
            return peak
        return self.base_rate

    def expected_count(self, t: float) -> float:
        """Mean number of arrivals in ``[0, t]`` = ``∫₀ᵗ λ(s) ds``."""
        t = max(0.0, min(t, self.duration_s))
        if self.shape is ArrivalShape.CONSTANT:
            return self.base_rate * t
        if self.shape is ArrivalShape.RAMP:
            r0 = self.start_rate if self.start_rate is not None else self.base_rate
            r1 = self.end_rate if self.end_rate is not None else self.base_rate
            # ∫ (r0 + (r1-r0) s/D) ds from 0..t  = r0 t + (r1-r0) t^2 / (2 D)
            return r0 * t + (r1 - r0) * t * t / (2.0 * self.duration_s)
        # SPIKE: base over the whole window plus the extra peak-base over [start,end]∩[0,t]
        base_part = self.base_rate * t
        peak = self.peak_rate or self.base_rate
        lo = self.spike_start_s
        hi = min(self.spike_end_s, t)
        extra = (peak - self.base_rate) * max(0.0, hi - lo) if t >= lo else 0.0
        return base_part + extra

    def total_expected(self) -> float:
        """Mean total arrivals over the whole run."""
        return self.expected_count(self.duration_s)


def deterministic_arrivals(env: RateEnvelope) -> list[float]:
    """Evenly-paced arrival times matching ``env``'s local rate (no RNG).

    Arrival ``k`` (1-based) is placed where the cumulative expected count first
    reaches ``k`` — i.e. ``expected_count(t_k) = k``. This makes the empirical
    local rate track ``λ(t)`` exactly without randomness, which is the cleanest
    way to test ramp/spike *shape*. We solve the inversion numerically by a
    monotone bisection on ``expected_count`` (it is non-decreasing), which works
    for every envelope including the piecewise spike.
    """
    total = env.total_expected()
    n = int(math.floor(total + 1e-9))
    if n <= 0:
        return []
    times: list[float] = []
    for k in range(1, n + 1):
        times.append(_invert_expected(env, float(k)))
    return times


def _invert_expected(env: RateEnvelope, target: float) -> float:
    """Smallest ``t`` in ``[0, duration]`` with ``expected_count(t) >= target``."""
    lo, hi = 0.0, env.duration_s
    # expected_count is monotone non-decreasing; 60 bisections gives ~1e-18 of
    # the duration, far below any latency resolution.
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if env.expected_count(mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def poisson_arrivals(env: RateEnvelope, rng: random.Random) -> list[float]:
    """Sample a (possibly non-homogeneous) Poisson arrival schedule.

    We sample a homogeneous Poisson process at the envelope's peak rate and
    thin it (Lewis–Shedler): accept a candidate arrival at ``t`` with probability
    ``λ(t) / λ_max``. The result is a correct sample of the non-homogeneous
    process whose intensity is ``env.rate_at``. Times are returned sorted.
    """
    lam_max = _peak_rate(env)
    if lam_max <= 0:
        return []
    times: list[float] = []
    t = 0.0
    while True:
        # Next homogeneous candidate gap ~ Exponential(lam_max).
        gap = rng.expovariate(lam_max)
        t += gap
        if t >= env.duration_s:
            break
        if rng.random() <= env.rate_at(t) / lam_max:
            times.append(t)
    return times


def _peak_rate(env: RateEnvelope) -> float:
    """An upper bound on ``λ(t)`` over the run (for thinning)."""
    if env.shape is ArrivalShape.CONSTANT:
        return env.base_rate
    if env.shape is ArrivalShape.RAMP:
        r0 = env.start_rate if env.start_rate is not None else env.base_rate
        r1 = env.end_rate if env.end_rate is not None else env.base_rate
        return max(r0, r1)
    return max(env.base_rate, env.peak_rate or env.base_rate)


#: A schedule generator: ``(env, rng) -> sorted intended-send times``.
ArrivalGenerator = Callable[[RateEnvelope, random.Random], Sequence[float]]


def make_schedule(
    env: RateEnvelope, *, poisson: bool, rng: random.Random
) -> list[float]:
    """Build the intended-send-time schedule for ``env``.

    ``poisson=True`` gives a realistic bursty stream; ``False`` gives the evenly
    paced deterministic placement (best for asserting shape in tests).
    """
    if poisson:
        return poisson_arrivals(env, rng)
    return deterministic_arrivals(env)
