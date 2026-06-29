"""Graceful load-shedding under saturation (kinora.md §4.4, §12.4).

When demand outruns the fleet faster than the autoscaler can warm capacity (a
burst, or a fleet pinned at its cap), something has to give. Shedding nothing
means *every* request — including the sacred committed-zone video the reader is
about to watch — queues behind a flood of speculative prefetch, and the buffer
stalls. The §4.4 degradation ladder is the product answer: under pressure, the
speculative zone degrades to a Ken-Burns still (zero generation), so shedding a
speculative render is *not* a failure — it is the designed graceful path.

This module is the admission controller that implements that priority-aware
shedding. Given the live load (a saturation signal) and an arriving request, it
decides **admit / shed**:

* **committed** requests are admitted as long as the fleet can physically take
  them (the buffer is never sacrificed);
* **speculative** requests are shed once a saturation signal crosses a threshold,
  using a *load-proportional* shed probability (shed gently at the knee, harder as
  saturation climbs) rather than a hard cliff, so the system degrades smoothly;
* an explicit hard cap on the global queue protects against unbounded growth.

The saturation signal is deliberately abstract — a single ``saturation`` float in
``[0, 1]`` (e.g. ``inflight / capacity``, or a normalised queue depth, or a burn-
rate) — so the same controller works against the simulator's pool state and the
live router metrics. Deterministic given a seeded RNG for the probabilistic shed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum

from app.inference.scaling.workload import RequestPriority

__all__ = [
    "AdmissionOutcome",
    "SheddingPolicy",
    "ShedDecision",
    "LoadShedder",
]


class AdmissionOutcome(StrEnum):
    """What the admission controller decided for a request."""

    ADMIT = "admit"
    SHED = "shed"  # rejected → client falls to the §4.4 Ken-Burns ladder


@dataclass(frozen=True, slots=True)
class SheddingPolicy:
    """When and how aggressively to shed speculative load."""

    #: Saturation below this never sheds (the healthy band).
    shed_knee: float = 0.75
    #: At/above this saturation, speculative shedding is total (prob 1).
    shed_ceiling: float = 0.98
    #: Hard cap on total admitted-but-unfinished work (protects against blowup).
    max_queue: int = 10_000
    #: Committed work is shed only when the fleet physically cannot take it.
    protect_committed: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.shed_knee < self.shed_ceiling <= 1.0:
            raise ValueError("require 0 <= shed_knee < shed_ceiling <= 1")
        if self.max_queue < 1:
            raise ValueError("max_queue must be >= 1")

    def shed_probability(self, saturation: float) -> float:
        """The probability a *speculative* request is shed at this saturation.

        Linear ramp from 0 at the knee to 1 at the ceiling (clamped) — a smooth
        degradation rather than a cliff, so the speculative zone thins gradually.
        """
        if saturation <= self.shed_knee:
            return 0.0
        if saturation >= self.shed_ceiling:
            return 1.0
        return (saturation - self.shed_knee) / (self.shed_ceiling - self.shed_knee)


@dataclass(frozen=True, slots=True)
class ShedDecision:
    """The admission verdict for one request."""

    outcome: AdmissionOutcome
    priority: RequestPriority
    saturation: float
    shed_probability: float
    reason: str

    @property
    def admitted(self) -> bool:
        return self.outcome is AdmissionOutcome.ADMIT

    def to_dict(self) -> dict[str, object]:
        """JSON projection."""
        return {
            "outcome": self.outcome.value,
            "priority": self.priority.value,
            "saturation": round(self.saturation, 4),
            "shed_probability": round(self.shed_probability, 4),
            "reason": self.reason,
        }


@dataclass
class LoadShedder:
    """Priority-aware admission controller (the §4.4 graceful degradation gate).

    Holds the seeded RNG for the probabilistic speculative shed so a simulation
    run is reproducible. ``admit`` is called per arrival with the current
    saturation + the global outstanding-work count.
    """

    policy: SheddingPolicy = field(default_factory=SheddingPolicy)
    seed: int = 0
    _rng: random.Random = field(init=False)
    #: Running totals for the report.
    admitted: int = 0
    shed: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def admit(
        self,
        *,
        priority: RequestPriority,
        saturation: float,
        outstanding: int,
        can_serve_now: bool = True,
    ) -> ShedDecision:
        """Decide admit/shed for one arriving request.

        ``outstanding`` is total admitted-but-unfinished work (vs. the hard cap);
        ``can_serve_now`` is whether the fleet has any free slot/headroom for a
        committed request right now (committed is only shed when physically full).
        """
        sat = max(0.0, min(1.0, saturation))

        # Hard global cap protects against unbounded queue growth — applies to all.
        if outstanding >= self.policy.max_queue:
            return self._record(
                ShedDecision(
                    outcome=AdmissionOutcome.SHED,
                    priority=priority,
                    saturation=sat,
                    shed_probability=1.0,
                    reason="global queue cap reached",
                )
            )

        if priority is RequestPriority.COMMITTED:
            if self.policy.protect_committed and not can_serve_now:
                # The fleet is physically full; even committed must wait — but we
                # admit it (it queues at the front via preemption), never shed it,
                # unless the global cap (above) forces it.
                return self._record(
                    ShedDecision(
                        outcome=AdmissionOutcome.ADMIT,
                        priority=priority,
                        saturation=sat,
                        shed_probability=0.0,
                        reason="committed admitted (queues; never shed)",
                    )
                )
            return self._record(
                ShedDecision(
                    outcome=AdmissionOutcome.ADMIT,
                    priority=priority,
                    saturation=sat,
                    shed_probability=0.0,
                    reason="committed admitted",
                )
            )

        # Speculative: probabilistic shed proportional to saturation past the knee.
        p = self.policy.shed_probability(sat)
        if p > 0.0 and self._rng.random() < p:
            return self._record(
                ShedDecision(
                    outcome=AdmissionOutcome.SHED,
                    priority=priority,
                    saturation=sat,
                    shed_probability=p,
                    reason="speculative shed under saturation (Ken-Burns ladder)",
                )
            )
        return self._record(
            ShedDecision(
                outcome=AdmissionOutcome.ADMIT,
                priority=priority,
                saturation=sat,
                shed_probability=p,
                reason="speculative admitted",
            )
        )

    def _record(self, decision: ShedDecision) -> ShedDecision:
        if decision.admitted:
            self.admitted += 1
        else:
            self.shed += 1
        return decision

    @property
    def shed_rate(self) -> float:
        """Fraction of all decisions that were shed (0 if none seen)."""
        total = self.admitted + self.shed
        return self.shed / total if total else 0.0
