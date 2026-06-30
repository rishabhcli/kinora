"""Worker-lane taxonomy + per-lane scaling policy (kinora.md §4.9, §12.2, §12.4).

The render fabric is **not one homogeneous worker pool**. The degradation ladder
(§12.4: full video -> Ken-Burns over a keyframe -> illustration -> narrated text)
and the agent crew run on physically different capacity with different cost and
different scaling physics:

* **CPU (Ken-Burns)** — the ffmpeg degradation lane. Cheap, fast to start, scales
  freely on commodity CPU. This is the *floor* that keeps the film from ever
  hard-stopping, so it gets generous bounds and a low cost weight.
* **PROVIDER (Wan / MiniMax / image-gen)** — workers that mostly *wait* on a hosted
  provider HTTP job. Their bottleneck is the provider's rate quota
  (``429 Throttling.RateQuota``), not local CPU, so concurrency is bounded by what
  the provider tolerates, and "in-flight provider jobs" is itself a scaling signal.
* **GPU** — any local accelerated lane (the optional local Wan TI2V tester, future
  on-prem inference). Scarce, slow to warm, expensive: the cost cap bites hardest
  here, and scale-in is the slowest.

Each lane has its own :class:`LanePool` policy: ``[min, max]`` bounds, a
target-tracking sensitivity (`jobs_per_worker` — the queue depth one worker is
expected to keep up with), a per-replica cost weight for the global cost cap, and
warm-up / drain dynamics (a GPU replica that takes 90s to become useful must not
be torn down 10s after it warms). Pure data + pure helpers; no clock, no infra.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

__all__ = [
    "Lane",
    "LanePool",
    "QoSClass",
    "default_lane_pools",
    "lane_for_qos",
]


class Lane(enum.StrEnum):
    """A physically distinct worker pool that scales independently."""

    #: ffmpeg Ken-Burns / degradation lane — commodity CPU, cheap, fast start.
    CPU = "cpu"
    #: Provider-bound workers (Wan / MiniMax / image-gen) — bounded by provider quota.
    PROVIDER = "provider"
    #: Local accelerated lane (GPU/MPS) — scarce, slow warm-up, expensive.
    GPU = "gpu"


class QoSClass(enum.StrEnum):
    """Quality-of-service class of a queued render job (maps to a render priority).

    Mirrors :class:`app.db.models.enums.RenderPriority` semantics without importing
    it, so the autoscaler is decoupled from the queue's DB enum:

    * ``COMMITTED`` — sacred buffer in front of the reader; never starved.
    * ``SPECULATIVE`` — droppable look-ahead; sheds first under pressure.
    * ``KEYFRAME`` — cheap image-gen for the speculative horizon.
    """

    COMMITTED = "committed"
    SPECULATIVE = "speculative"
    KEYFRAME = "keyframe"


@dataclass(frozen=True, slots=True)
class LanePool:
    """Scaling policy + cost model for one :class:`Lane`.

    Attributes:
        lane: which physical pool this policy governs.
        min_replicas: floor — the §4.9 steady-state cap is the *minimum* warm pool.
        max_replicas: hard ceiling regardless of demand (capacity guard rail).
        jobs_per_worker: target backlog one replica is sized to keep up with; the
            target-tracking term is ``ceil(backlog / jobs_per_worker)``.
        cost_per_replica: relative $/hour weight, charged against the global cost
            cap. CPU is cheap (~1), provider moderate, GPU dear.
        warmup_s: time a freshly added replica takes before it is useful. The
            controller will not credit nor tear down a replica younger than this.
        scale_in_step: max replicas removed per scale-in decision (graceful drain;
            scarce lanes drain one at a time).
        scale_out_step: max replicas added per scale-out decision (0 = unbounded
            jump straight to target; >0 ramps to avoid over-provisioning on a spike).
        elastic: when False the pool is pinned at ``min_replicas`` (e.g. a fixed
            keyframe lane) and demand never moves it.
    """

    lane: Lane
    min_replicas: int
    max_replicas: int
    jobs_per_worker: float = 2.0
    cost_per_replica: float = 1.0
    warmup_s: float = 0.0
    scale_in_step: int = 1_000_000
    scale_out_step: int = 0
    elastic: bool = True

    def __post_init__(self) -> None:
        if self.min_replicas < 0:
            raise ValueError("min_replicas must be >= 0")
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        if self.jobs_per_worker <= 0:
            raise ValueError("jobs_per_worker must be > 0")
        if self.cost_per_replica < 0:
            raise ValueError("cost_per_replica must be >= 0")
        if self.warmup_s < 0:
            raise ValueError("warmup_s must be >= 0")
        if self.scale_in_step < 1:
            raise ValueError("scale_in_step must be >= 1")
        if self.scale_out_step < 0:
            raise ValueError("scale_out_step must be >= 0")

    def clamp(self, replicas: float) -> int:
        """Clamp a desired replica count into ``[min, max]`` and round up."""
        n = math.ceil(replicas - 1e-9)
        return max(self.min_replicas, min(self.max_replicas, n))

    def target_for_backlog(self, backlog: float) -> int:
        """Pure target-tracking term: replicas to drain ``backlog`` jobs.

        Returns ``min_replicas`` when inelastic or when there is no backlog.
        """
        if not self.elastic:
            return self.min_replicas
        if backlog <= 0:
            return self.min_replicas
        want = math.ceil(backlog / self.jobs_per_worker)
        return self.clamp(float(want))

    def cost_at(self, replicas: int) -> float:
        """Cost weight of running ``replicas`` of this lane."""
        return self.cost_per_replica * max(0, replicas)


#: QoS-class -> physical lane routing. Committed/speculative video both land on
#: the provider lane (hosted Wan/MiniMax); the CPU lane is the Ken-Burns fallback
#: that any QoS can degrade onto; keyframes are cheap image-gen, also provider.
_QOS_TO_LANE: dict[QoSClass, Lane] = {
    QoSClass.COMMITTED: Lane.PROVIDER,
    QoSClass.SPECULATIVE: Lane.PROVIDER,
    QoSClass.KEYFRAME: Lane.PROVIDER,
}


def lane_for_qos(qos: QoSClass) -> Lane:
    """Physical lane that serves a QoS class at full fidelity."""
    return _QOS_TO_LANE[qos]


def default_lane_pools(
    *,
    cpu_min: int = 2,
    cpu_max: int = 24,
    provider_min: int = 4,
    provider_max: int = 16,
    gpu_min: int = 0,
    gpu_max: int = 4,
) -> dict[Lane, LanePool]:
    """The §4.9 caps as elastic lane pools with realistic cost/warm-up physics.

    Defaults encode: CPU cheap + elastic + fast; PROVIDER moderate + quota-bounded;
    GPU scarce + expensive + slow to warm and slow to drain. The §4.9 steady-state
    slots (4 committed + 2 speculative) live as the PROVIDER lane's minimum.
    """
    return {
        Lane.CPU: LanePool(
            lane=Lane.CPU,
            min_replicas=cpu_min,
            max_replicas=cpu_max,
            jobs_per_worker=3.0,
            cost_per_replica=1.0,
            warmup_s=5.0,
            scale_out_step=0,  # CPU starts fast: jump to target
        ),
        Lane.PROVIDER: LanePool(
            lane=Lane.PROVIDER,
            min_replicas=provider_min,
            max_replicas=provider_max,
            jobs_per_worker=2.0,
            cost_per_replica=4.0,
            warmup_s=15.0,
            scale_out_step=4,  # ramp toward target so we don't slam the quota
        ),
        Lane.GPU: LanePool(
            lane=Lane.GPU,
            min_replicas=gpu_min,
            max_replicas=gpu_max,
            jobs_per_worker=1.0,
            cost_per_replica=20.0,
            warmup_s=90.0,
            scale_in_step=1,  # drain one GPU at a time
            scale_out_step=1,  # warm one GPU at a time (expensive to over-provision)
        ),
    }
