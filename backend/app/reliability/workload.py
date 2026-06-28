"""Workload models + ramp profiles — open vs. closed load (kinora.md §4/§12.2).

Reliability testing distinguishes two load *models*, and conflating them is the
classic mistake (the "coordinated-omission" trap):

* **Closed model** — a fixed population of ``N`` virtual users, each looping
  *request → think → request*. Concurrency is capped at ``N``; if the server
  slows, users simply wait, so offered load self-throttles. This matches a fixed
  set of readers each driving one session (the natural Kinora shape).
* **Open model** — requests arrive from an *unbounded* population at a target
  rate ``λ`` (req/s), independent of how fast the server responds. Arrivals are a
  Poisson process (exponential inter-arrival gaps). This is the model that
  actually exposes backpressure (§12.2) and queueing collapse, because a slow
  server cannot slow the arrivals.

A **ramp profile** modulates the offered rate / population over time
(constant / linear warm-up / step / spike), so a run can warm up, hold, and
spike without bespoke code per scenario.

Everything here is pure schedule math over an injected seeded RNG — it produces
arrival *timestamps* and per-phase concurrency, which the async runner turns into
real requests. The unit tests pin the arrival counts, the rate shaping, and the
Poisson mean against the seed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum


class WorkloadKind(StrEnum):
    """Which load model a scenario uses."""

    CLOSED = "closed"  # N looping virtual users (think-time paced)
    OPEN = "open"  # Poisson arrivals at a target rate


# --------------------------------------------------------------------------- #
# Ramp profiles — rate(t) in [0, 1] scaling factor over the run
# --------------------------------------------------------------------------- #


class RampShape(StrEnum):
    """The shape of the offered-load envelope over the run."""

    CONSTANT = "constant"
    LINEAR = "linear"  # warm up linearly from ``floor`` to 1.0 over ``ramp_s``
    STEP = "step"  # jump to 1.0 after ``ramp_s``
    SPIKE = "spike"  # baseline, then a transient multiplier over a window


@dataclass(frozen=True, slots=True)
class RampProfile:
    """A time-varying scaling factor on the offered load (multiplier on rate/users).

    ``factor(t)`` returns a non-negative multiplier (usually in ``[floor, peak]``)
    for model-time ``t`` seconds into the run. The runner multiplies the base
    rate (open) or rounds the base population (closed) by this factor.
    """

    shape: RampShape = RampShape.CONSTANT
    #: Warm-up / step duration in seconds (ignored by CONSTANT).
    ramp_s: float = 5.0
    #: Floor multiplier at t=0 for LINEAR/STEP (e.g. start at 10% load).
    floor: float = 0.1
    #: Spike multiplier and window (SPIKE only).
    spike_mult: float = 3.0
    spike_start_s: float = 10.0
    spike_len_s: float = 5.0

    def factor(self, t_s: float) -> float:
        """The offered-load multiplier at model-time ``t_s`` (>= 0)."""
        t = max(0.0, t_s)
        if self.shape is RampShape.CONSTANT:
            return 1.0
        if self.shape is RampShape.LINEAR:
            if self.ramp_s <= 0.0:
                return 1.0
            frac = min(1.0, t / self.ramp_s)
            return self.floor + (1.0 - self.floor) * frac
        if self.shape is RampShape.STEP:
            return 1.0 if t >= self.ramp_s else self.floor
        # SPIKE: baseline 1.0, with a transient bump in the window.
        if self.spike_start_s <= t < self.spike_start_s + self.spike_len_s:
            return self.spike_mult
        return 1.0


# --------------------------------------------------------------------------- #
# Open model — Poisson arrival schedule
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OpenWorkload:
    """An open-model arrival process at a base rate, shaped by a ramp (§12.2).

    The arrival process is a (possibly time-varying) Poisson process: between
    arrivals we wait an exponential gap whose mean is ``1 / (base_rate_rps *
    ramp.factor(t))``. A thinning approach keeps it correct under the ramp: we
    generate candidate arrivals at the *peak* rate and accept each with
    probability ``rate(t) / peak_rate`` (the standard Lewis–Shedler method),
    so the realised intensity tracks the ramp exactly.
    """

    base_rate_rps: float
    ramp: RampProfile = RampProfile()
    seed: int = 0

    def _peak_rate(self, duration_s: float) -> float:
        """An upper bound on the time-varying rate over the run (for thinning)."""
        peak_factor = max(self.ramp.factor(0.0), self.ramp.factor(duration_s))
        if self.ramp.shape is RampShape.SPIKE:
            peak_factor = max(peak_factor, self.ramp.spike_mult)
        if self.ramp.shape is RampShape.LINEAR:
            peak_factor = max(peak_factor, 1.0)
        return self.base_rate_rps * max(peak_factor, 1e-9)

    def arrival_times(self, *, duration_s: float) -> list[float]:
        """The accepted arrival timestamps (seconds) over ``[0, duration_s)``."""
        if self.base_rate_rps <= 0.0 or duration_s <= 0.0:
            return []
        rng = random.Random(self.seed)
        peak = self._peak_rate(duration_s)
        times: list[float] = []
        t = 0.0
        while True:
            # Candidate at the peak rate (homogeneous Poisson).
            t += rng.expovariate(peak)
            if t >= duration_s:
                break
            # Thin: accept with prob rate(t)/peak so the intensity follows the ramp.
            rate_t = self.base_rate_rps * self.ramp.factor(t)
            if rng.random() < rate_t / peak:
                times.append(t)
        return times

    def expected_arrivals(self, *, duration_s: float) -> float:
        """The analytic mean arrival count = integral of rate(t) dt (for tests)."""
        if self.base_rate_rps <= 0.0 or duration_s <= 0.0:
            return 0.0
        # Numerically integrate rate(t) over the window (fine grid).
        steps = 2000
        dt = duration_s / steps
        total = 0.0
        for i in range(steps):
            t = (i + 0.5) * dt
            total += self.base_rate_rps * self.ramp.factor(t) * dt
        return total


# --------------------------------------------------------------------------- #
# Closed model — N looping users with think-time
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ThinkTime:
    """The think-time distribution between a user's successive actions.

    A user's pacing is *not* fixed: a real reader dwells variably between intent
    updates. ``sample`` draws a non-negative gap (seconds) from a clamped normal,
    so the closed-model concurrency is realistic rather than lockstep.
    """

    mean_s: float = 1.0
    jitter_s: float = 0.3
    min_s: float = 0.05

    def sample(self, rng: random.Random) -> float:
        """Draw one think-time gap (seconds), clamped to ``>= min_s``."""
        if self.jitter_s <= 0.0:
            return max(self.min_s, self.mean_s)
        return max(self.min_s, rng.gauss(self.mean_s, self.jitter_s))


@dataclass(frozen=True, slots=True)
class ClosedWorkload:
    """A closed model: ``users`` looping virtual users shaped by a ramp.

    The active population at model-time ``t`` is ``round(users * ramp.factor(t))``
    (clamped to ``[0, users]``) — a linear ramp warms users up gradually, a step
    brings them all on after the warm-up. The per-user think-time governs how
    often each looping user issues its next action.
    """

    users: int
    think: ThinkTime = ThinkTime()
    ramp: RampProfile = RampProfile()

    def active_users(self, t_s: float) -> int:
        """The number of active virtual users at model-time ``t_s``."""
        scaled = round(self.users * self.ramp.factor(t_s))
        return max(0, min(self.users, scaled))


# --------------------------------------------------------------------------- #
# A unified workload plan the runner consumes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkloadPlan:
    """A complete offered-load specification for one run.

    Exactly one of ``open_model`` / ``closed_model`` is set, selected by ``kind``.
    ``duration_s`` is the run length; the runner honours the ramp inside it.
    """

    kind: WorkloadKind
    duration_s: float
    open_model: OpenWorkload | None = None
    closed_model: ClosedWorkload | None = None

    def __post_init__(self) -> None:
        if self.kind is WorkloadKind.OPEN and self.open_model is None:
            raise ValueError("open workload requires open_model")
        if self.kind is WorkloadKind.CLOSED and self.closed_model is None:
            raise ValueError("closed workload requires closed_model")

    @classmethod
    def open(
        cls,
        *,
        rate_rps: float,
        duration_s: float,
        ramp: RampProfile | None = None,
        seed: int = 0,
    ) -> WorkloadPlan:
        """Build an open-model plan at ``rate_rps`` for ``duration_s`` seconds."""
        return cls(
            kind=WorkloadKind.OPEN,
            duration_s=duration_s,
            open_model=OpenWorkload(
                base_rate_rps=rate_rps, ramp=ramp or RampProfile(), seed=seed
            ),
        )

    @classmethod
    def closed(
        cls,
        *,
        users: int,
        duration_s: float,
        think: ThinkTime | None = None,
        ramp: RampProfile | None = None,
    ) -> WorkloadPlan:
        """Build a closed-model plan with ``users`` virtual users."""
        return cls(
            kind=WorkloadKind.CLOSED,
            duration_s=duration_s,
            closed_model=ClosedWorkload(
                users=users, think=think or ThinkTime(), ramp=ramp or RampProfile()
            ),
        )

    def describe(self) -> dict[str, float | int | str]:
        """A flat description for the report header / `--dry-run`."""
        base: dict[str, float | int | str] = {
            "kind": self.kind.value,
            "duration_s": self.duration_s,
        }
        if self.open_model is not None:
            base["rate_rps"] = self.open_model.base_rate_rps
            base["ramp"] = self.open_model.ramp.shape.value
            base["expected_arrivals"] = round(
                self.open_model.expected_arrivals(duration_s=self.duration_s), 2
            )
        if self.closed_model is not None:
            base["users"] = self.closed_model.users
            base["ramp"] = self.closed_model.ramp.shape.value
            base["think_mean_s"] = self.closed_model.think.mean_s
        return base


def windowed_rate(
    arrival_times: Sequence[float], *, window_s: float, duration_s: float
) -> Iterator[tuple[float, float]]:
    """Yield ``(window_start_s, rate_rps)`` over fixed windows (a rate timeline).

    Used to verify an arrival schedule tracks its ramp (and for a report's
    offered-rate sparkline). Empty windows yield a rate of ``0.0``.
    """
    if window_s <= 0.0:
        raise ValueError("window_s must be positive")
    n_windows = max(1, math.ceil(duration_s / window_s))
    counts = [0] * n_windows
    for t in arrival_times:
        idx = min(n_windows - 1, int(t / window_s))
        if 0 <= idx < n_windows:
            counts[idx] += 1
    for i, c in enumerate(counts):
        yield i * window_s, c / window_s


__all__ = [
    "ClosedWorkload",
    "OpenWorkload",
    "RampProfile",
    "RampShape",
    "ThinkTime",
    "WorkloadKind",
    "WorkloadPlan",
    "windowed_rate",
]
