"""The QoS work-item model — render work as schedulable units (kinora.md §4.9/§12.2).

The render queue's three lanes (committed > speculative > keyframe, §12.1) become
**QoS classes** here. A :class:`QoSItem` is one render job described *only* by the
properties a scheduling policy needs — its class, the book/session/tenant it
belongs to, when it was enqueued, when the reader needs it (its deadline), and a
*value* estimate for load-shedding. The fabric never touches Redis or the real
``QueuedJob``; an adapter maps one onto the other so the policy stays pure and
unit-testable against a virtual clock + synthetic load.

The mapping to the live system:

* :class:`QoSClass.COMMITTED` ↔ ``RenderPriority.COMMITTED`` — the near-reader
  zone; strict top priority, never shed, never starved.
* :class:`QoSClass.SPECULATIVE` ↔ ``RenderPriority.SPECULATIVE`` — droppable,
  preemptible work ahead of the commit horizon.
* :class:`QoSClass.COLD` ↔ ``RenderPriority.KEYFRAME`` — the cheap, distant
  plan/keyframe lane; lowest priority but **must not fully starve** (the WFQ
  weight + aging guarantee that).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models.enums import RenderPriority


class QoSClass(IntEnum):
    """The three QoS classes, ordered by strict priority (lower int = higher).

    ``IntEnum`` so ``min(...)`` / sorting gives the strict-priority order for free:
    ``COMMITTED < SPECULATIVE < COLD``.
    """

    COMMITTED = 0
    SPECULATIVE = 1
    COLD = 2

    @property
    def droppable(self) -> bool:
        """Committed work is never load-shed; speculative/cold are."""
        return self is not QoSClass.COMMITTED

    @property
    def preemptible(self) -> bool:
        """Committed work is never preempted; speculative/cold are."""
        return self is not QoSClass.COMMITTED

    @classmethod
    def from_priority(cls, priority: RenderPriority | str) -> QoSClass:
        """Map a ``RenderPriority`` (or its value) onto a QoS class."""
        value = getattr(priority, "value", priority)
        return _PRIORITY_TO_CLASS[str(value)]

    def to_priority_value(self) -> str:
        """The ``RenderPriority`` lane value this class drains into."""
        return _CLASS_TO_PRIORITY[self]


_PRIORITY_TO_CLASS: dict[str, QoSClass] = {
    "committed": QoSClass.COMMITTED,
    "speculative": QoSClass.SPECULATIVE,
    "keyframe": QoSClass.COLD,
}

_CLASS_TO_PRIORITY: dict[QoSClass, str] = {
    QoSClass.COMMITTED: "committed",
    QoSClass.SPECULATIVE: "speculative",
    QoSClass.COLD: "keyframe",
}


class SheddingReason(StrEnum):
    """Why a load-shed / admission policy acted (machine-readable telemetry)."""

    SHED_LEAST_VALUE = "shed_least_value"
    SHED_OVER_DEADLINE = "shed_over_deadline"
    SHED_BACKPRESSURE = "shed_backpressure"
    SHED_TENANT_OVER_FAIR_SHARE = "shed_tenant_over_fair_share"
    DEFER_SATURATED = "defer_saturated"
    REJECT_SATURATED = "reject_saturated"
    ADMIT = "admit"


@dataclass(slots=True)
class QoSItem:
    """One unit of render work, described for scheduling only.

    All times are monotonic float seconds on the fabric's :class:`~app.qos.clock.Clock`.
    ``deadline`` is the absolute time by which the reader needs the clip (its span
    ETA + ``now``); ``None`` means no hard deadline (cold/plan work). ``value`` is a
    relative worth used to shed the *least* valuable speculative work first — a
    higher number is more worth keeping. ``cost_s`` is the video-seconds (or
    render-seconds) this item is expected to consume, used by value-density shedding.
    """

    id: str
    qos_class: QoSClass
    book_id: str
    enqueued_at: float
    session_id: str | None = None
    #: The tenant/owner for cross-book fairness; defaults to the book itself.
    tenant_id: str | None = None
    deadline: float | None = None
    value: float = 1.0
    cost_s: float = 5.0
    #: Reading-time ETA in seconds at enqueue (informational; deadline drives EDF).
    eta_s: float | None = None
    #: Bumped by the aging policy; effective priority = class - aging_boost.
    aging_boost: int = field(default=0)

    @property
    def fairness_key(self) -> str:
        """The key cross-tenant fairness accounts against (tenant, else book)."""
        return self.tenant_id or self.book_id

    def wait_s(self, now: float) -> float:
        """How long this item has waited in queue (never negative)."""
        return max(0.0, now - self.enqueued_at)

    def slack_s(self, now: float) -> float | None:
        """Time until the deadline (negative = already late); ``None`` if no deadline."""
        if self.deadline is None:
            return None
        return self.deadline - now

    def value_density(self) -> float:
        """Value per video-second — the metric load-shedding minimises over.

        Shedding the lowest value-density work first frees the most contended
        capacity per unit of reader value lost.
        """
        return self.value / max(self.cost_s, 1e-9)


__all__ = ["QoSClass", "QoSItem", "SheddingReason"]
