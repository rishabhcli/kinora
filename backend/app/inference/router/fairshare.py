"""Priority + weighted-fair-share scheduling across tenants/agents (§12.2).

Two layers, in order:

1. **Strict priority classes.** ``INTERACTIVE > COMMITTED > SPECULATIVE > BULK``.
   A higher class is always served before any lower one — committed live work
   never waits behind speculative prefetch or offline bulk. Preemption of
   *running* speculative work is the router's job; this scheduler governs *queue
   ordering*, which is the half that decides fairness.

2. **Weighted fair share *within* a class**, by ``(tenant, agent)`` flow. This is
   a virtual-time WFQ (a.k.a. deficit-free SFQ): each flow has a *virtual finish
   time* that advances by ``cost / weight`` every time it is served, so a flow
   with weight 2 gets twice the throughput of a weight-1 flow, and a flow that
   has consumed more than its share is deprioritised until others catch up. A
   flow that goes idle and returns is re-based to the current virtual time so it
   neither hoards a backlog credit nor is punished for having been quiet — the
   property a naive round-robin lacks.

This is a *pure* data structure: deterministic, no clock, no I/O. The router
feeds it ready requests and pops the next one to dispatch. Fairness is therefore
unit-testable in isolation and is exactly what the simulator asserts on.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .errors import RouterConfigError
from .request import InferenceRequest, RequestPriority


@dataclass
class _Flow:
    """Per-(tenant, agent) WFQ state."""

    key: tuple[str, str]
    weight: float
    virtual_time: float = 0.0
    #: FIFO of (seq, request) so ties within a flow keep arrival order.
    queue: list[tuple[int, InferenceRequest]] = field(default_factory=list)
    served_cost: float = 0.0
    served_count: int = 0

    @property
    def backlogged(self) -> bool:
        return bool(self.queue)


@dataclass(frozen=True, slots=True)
class FairShareConfig:
    """Tunables for the fair-share scheduler.

    Attributes:
        default_weight: Weight assigned to a flow with no explicit weight.
        tenant_weights: Per-tenant weight overrides (applied to every agent flow
            under that tenant unless a more specific ``flow_weights`` entry wins).
        flow_weights: Per-(tenant, agent) weight overrides; most specific.
    """

    default_weight: float = 1.0
    tenant_weights: dict[str, float] = field(default_factory=dict)
    flow_weights: dict[tuple[str, str], float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default_weight <= 0:
            raise RouterConfigError("default_weight must be positive")
        for w in (*self.tenant_weights.values(), *self.flow_weights.values()):
            if w <= 0:
                raise RouterConfigError("all weights must be positive")

    def weight_for(self, key: tuple[str, str]) -> float:
        if key in self.flow_weights:
            return self.flow_weights[key]
        return self.tenant_weights.get(key[0], self.default_weight)


class FairShareScheduler:
    """Strict-priority over per-flow weighted-fair-share queue ordering."""

    def __init__(self, config: FairShareConfig | None = None) -> None:
        self.config = config or FairShareConfig()
        #: One WFQ universe per priority class.
        self._classes: dict[RequestPriority, dict[tuple[str, str], _Flow]] = {}
        #: Per-class virtual time (the "system" virtual clock).
        self._class_vtime: dict[RequestPriority, float] = {}
        self._seq = itertools.count()
        self._size = 0

    def __len__(self) -> int:
        return self._size

    @property
    def empty(self) -> bool:
        return self._size == 0

    # -- enqueue ---------------------------------------------------------- #

    def enqueue(self, request: InferenceRequest) -> None:
        """Add a ready request to its priority class + flow."""
        cls = request.priority
        flows = self._classes.setdefault(cls, {})
        vnow = self._class_vtime.setdefault(cls, 0.0)
        key = request.fairness_key()
        flow = flows.get(key)
        if flow is None:
            flow = _Flow(key=key, weight=self.config.weight_for(key))
            flows[key] = flow
        if not flow.backlogged:
            # Re-base an idle flow to "now" so it gets neither backlog credit nor
            # penalty for the period it was quiet (the WFQ idle-fairness rule).
            flow.virtual_time = max(flow.virtual_time, vnow)
        flow.queue.append((next(self._seq), request))
        self._size += 1

    # -- dequeue ---------------------------------------------------------- #

    def peek(self) -> InferenceRequest | None:
        """The request that would be dispatched next, without removing it."""
        cls = self._top_class()
        if cls is None:
            return None
        flow = self._min_vtime_flow(cls)
        return flow.queue[0][1] if flow is not None else None

    def pop(self) -> InferenceRequest | None:
        """Select and *commit* the next request to dispatch (priority then WFQ).

        Equivalent to :meth:`select_next` followed by :meth:`commit`. This is the
        simple "always dispatch the head" path used by direct callers/tests; a
        capacity-aware scheduler (the router) instead calls :meth:`select_next`
        with a ``skip`` set of flows whose head cannot currently be placed, and
        only :meth:`commit`\\ s the request it actually dispatches — so virtual
        time and served-cost advance on *genuine* service, never on a transient
        peek that gets re-queued.
        """
        request = self.select_next()
        if request is None:
            return None
        self.commit(request)
        return request

    def select_next(
        self, skip_flows: set[tuple[str, str]] | None = None
    ) -> InferenceRequest | None:
        """Return the next request to dispatch *without* removing/charging it.

        ``skip_flows`` lets the router pass over flows whose head-of-line request
        cannot currently be placed (e.g. too big for any worker's headroom this
        tick), so a blocked flow doesn't stall a placeable one — while still
        respecting strict priority *within* the placeable set.
        """
        skip = skip_flows or set()
        for cls in self._classes_high_to_low():
            flow = self._min_vtime_flow(cls, skip)
            if flow is not None:
                return flow.queue[0][1]
        return None

    def commit(self, request: InferenceRequest) -> None:
        """Remove ``request`` from its flow and advance fair-share accounting.

        Charges the flow's virtual finish time by ``cost / weight`` (the WFQ
        update) and records served cost/count. Must be called with a request
        returned by :meth:`select_next`; raises if it is not the flow's head.
        """
        cls = request.priority
        key = request.fairness_key()
        flow = self._classes.get(cls, {}).get(key)
        if flow is None or not flow.queue or flow.queue[0][1] is not request:
            raise RouterConfigError("commit() must be called on a flow's selected head")
        flow.queue.pop(0)
        self._size -= 1
        cost = request.share_cost()
        prev_vtime = flow.virtual_time
        flow.virtual_time += cost / flow.weight
        flow.served_cost += cost
        flow.served_count += 1
        self._class_vtime[cls] = max(self._class_vtime[cls], prev_vtime)

    def remove(self, request_id: str) -> bool:
        """Drop a queued request by id (used for SLA expiry / cancellation).

        Returns ``True`` if it was present. O(flows + queue) — fine for the
        modest queue depths a per-model router holds.
        """
        for flows in self._classes.values():
            for flow in flows.values():
                for i, (_, req) in enumerate(flow.queue):
                    if req.request_id == request_id:
                        del flow.queue[i]
                        self._size -= 1
                        return True
        return False

    def evict_victim(self, at_or_below: RequestPriority) -> InferenceRequest | None:
        """Remove + return a preemption victim at or below ``at_or_below`` (§12.2).

        Picks the **lowest priority class** with a backlog (so bulk is sacrificed
        before speculative), and within it the flow with the **largest virtual
        time** (the flow that has consumed the most of its share — the fairest to
        cut), evicting that flow's *most recently enqueued* request (LIFO, so the
        oldest queued work is preserved). Returns ``None`` if nothing at or below
        ``at_or_below`` is queued. The evicted request is *not* charged served cost.
        """
        for cls in sorted(self._classes):  # low to high
            if cls > at_or_below:
                break
            flows = self._classes[cls]
            victim_flow: _Flow | None = None
            for flow in flows.values():
                if flow.backlogged and (
                    victim_flow is None or flow.virtual_time > victim_flow.virtual_time
                ):
                    victim_flow = flow
            if victim_flow is not None:
                _, request = victim_flow.queue.pop()
                self._size -= 1
                return request
        return None

    # -- introspection ---------------------------------------------------- #

    def queued_requests(self) -> list[InferenceRequest]:
        """Every queued request (any class/flow), in no particular order."""
        out: list[InferenceRequest] = []
        for flows in self._classes.values():
            for flow in flows.values():
                out.extend(req for _, req in flow.queue)
        return out

    def served_cost_by_flow(self) -> dict[tuple[str, str], float]:
        """Cumulative served cost per flow (for fairness assertions / metrics)."""
        out: dict[tuple[str, str], float] = {}
        for flows in self._classes.values():
            for key, flow in flows.items():
                out[key] = out.get(key, 0.0) + flow.served_cost
        return out

    def depth_by_priority(self) -> dict[RequestPriority, int]:
        out: dict[RequestPriority, int] = {}
        for cls, flows in self._classes.items():
            out[cls] = sum(len(f.queue) for f in flows.values())
        return {k: v for k, v in out.items() if v}

    # -- internals -------------------------------------------------------- #

    def _top_class(self) -> RequestPriority | None:
        """Highest priority class with any backlog."""
        best: RequestPriority | None = None
        for cls, flows in self._classes.items():
            if any(f.backlogged for f in flows.values()) and (best is None or cls > best):
                best = cls
        return best

    def _classes_high_to_low(self) -> list[RequestPriority]:
        """Priority classes that hold any backlog, highest first."""
        return sorted(
            (
                cls
                for cls, flows in self._classes.items()
                if any(f.backlogged for f in flows.values())
            ),
            reverse=True,
        )

    def _min_vtime_flow(
        self, cls: RequestPriority, skip: set[tuple[str, str]] | None = None
    ) -> _Flow | None:
        """The backlogged flow in ``cls`` with the smallest virtual finish time.

        Flows in ``skip`` are ignored (their head can't be placed this tick).
        Ties break on the oldest head-of-line sequence number, so equal-weight
        flows interleave in stable arrival order rather than by dict insertion.
        """
        skip = skip or set()
        best: _Flow | None = None
        best_key: tuple[float, int] | None = None
        for key, flow in self._classes[cls].items():
            if not flow.backlogged or key in skip:
                continue
            head_seq = flow.queue[0][0]
            cand = (flow.virtual_time, head_seq)
            if best_key is None or cand < best_key:
                best, best_key = flow, cand
        return best


__all__ = ["FairShareConfig", "FairShareScheduler"]
