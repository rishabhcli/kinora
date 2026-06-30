"""Weighted fair queuing across QoS classes — deficit round robin (kinora.md §4.9).

Strict priority guarantees committed work always wins, but pure strict priority
**starves** the cold lane under sustained load. The fabric's dispatch is therefore
a *hybrid*: a strict committed reservation (those slots only ever serve committed)
plus a weighted-fair tier over the remaining slots so speculative and cold each get
a guaranteed slice in proportion to their WFQ weight. Cold never goes to zero.

This module implements the weighted allocator as **deficit round robin (DRR)**: a
classic packet-scheduler that hands each class a credit proportional to its weight
each round and serves an item when the class has accrued enough credit. DRR is
order-fair and starvation-free for any positive weight, and it's pure arithmetic —
perfect for deterministic tests.

The output is an *allocation* of the available service slots to classes; the
:mod:`app.qos.scheduler` then pops actual items from each class in EDF/age order.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.qos.config import QoSConfig
from app.qos.model import QoSClass

#: The classes the WFQ tier arbitrates, in tie-break (strict) order.
_WFQ_ORDER: tuple[QoSClass, ...] = (
    QoSClass.COMMITTED,
    QoSClass.SPECULATIVE,
    QoSClass.COLD,
)


@dataclass(frozen=True, slots=True)
class WFQAllocation:
    """How many service slots each class is granted this dispatch round."""

    per_class: dict[QoSClass, int]

    def get(self, qos_class: QoSClass) -> int:
        return self.per_class.get(qos_class, 0)

    @property
    def total(self) -> int:
        return sum(self.per_class.values())


def allocate_slots(
    *,
    available_slots: int,
    backlog: dict[QoSClass, int],
    config: QoSConfig,
) -> WFQAllocation:
    """Split ``available_slots`` across classes: committed reservation, anti-starvation
    floor, then weighted-fair remainder.

    1. **Committed reservation.** Committed claims up to ``committed_reserved_slots``
       of its backlog first (capped by what's available) — its near-reader latency
       is protected regardless of how much speculative/cold work is queued.
    2. **Anti-starvation floor.** Each *backlogged* class is then guaranteed one slot
       (in strict priority order, while slots remain). This is the per-round teeth of
       the "cold never fully starves" guarantee: as long as a single slot is left
       after committed's reservation, cold gets one even when committed/speculative
       are flooded — strict priority alone would zero it out, the floor won't.
    3. **Weighted-fair remainder.** Whatever is left is dealt by deficit round robin
       in proportion to ``wfq_weights`` (one slot per credit-threshold crossing, so a
       heavy class wins more *turns* without grabbing the whole round). Over many
       rounds the cumulative service tracks the weights.

    ``backlog`` is the number of *ready* (admitted, not-yet-dispatched) items per
    class. Allocation never exceeds a class's backlog (work-conserving: no slot is
    handed to an empty class while another class still has work).
    """
    granted: dict[QoSClass, int] = dict.fromkeys(_WFQ_ORDER, 0)
    remaining_backlog = {c: max(0, backlog.get(c, 0)) for c in _WFQ_ORDER}
    slots = max(0, available_slots)

    # 1. strict committed reservation
    reserve = min(config.committed_reserved_slots, remaining_backlog[QoSClass.COMMITTED], slots)
    granted[QoSClass.COMMITTED] += reserve
    remaining_backlog[QoSClass.COMMITTED] -= reserve
    slots -= reserve

    # 2. anti-starvation floor: one slot to each backlogged class while slots remain.
    backlogged = [c for c in _WFQ_ORDER if remaining_backlog[c] > 0]
    if slots > 0 and len(backlogged) > 1:
        for c in _WFQ_ORDER:
            if slots <= 0:
                break
            if remaining_backlog[c] > 0:
                granted[c] += 1
                remaining_backlog[c] -= 1
                slots -= 1

    # 3. weighted-fair remainder via deficit round robin.
    #
    # Credit accrues per class proportional to its weight, but a class is granted at
    # most **one** slot per visit even when it has accrued more — that one-per-visit
    # rule keeps the round-robin fair: a high-weight class wins more *turns* (it
    # crosses the credit threshold more often) but never grabs the whole round in a
    # single pass, so lower-weight classes always get their interleaved turns. Weights
    # are normalised so the largest weight accrues exactly 1.0 credit per pass.
    if slots > 0 and any(remaining_backlog[c] > 0 for c in _WFQ_ORDER):
        max_weight = max(config.weight(c) for c in _WFQ_ORDER)
        quantum = {c: config.weight(c) / max_weight for c in _WFQ_ORDER}
        deficit = dict.fromkeys(_WFQ_ORDER, 0.0)
        # Each pass can grant at most len(classes) slots and always accrues credit, so
        # the loop terminates; the guard is belt-and-braces against pathological input.
        guard = 0
        max_guard = (slots + 1) * (len(_WFQ_ORDER) + 1) * 4
        while slots > 0 and any(remaining_backlog[c] > 0 for c in _WFQ_ORDER):
            guard += 1
            if guard > max_guard:
                break
            for c in _WFQ_ORDER:
                if slots <= 0:
                    break
                if remaining_backlog[c] <= 0:
                    continue
                deficit[c] += quantum[c]
                if deficit[c] >= 1.0:
                    granted[c] += 1
                    remaining_backlog[c] -= 1
                    deficit[c] -= 1.0
                    slots -= 1
    return WFQAllocation(per_class=granted)


def fair_share_fractions(config: QoSConfig) -> dict[QoSClass, float]:
    """Each class's normalised WFQ weight (its long-run share of the fair tier)."""
    total = sum(config.weight(c) for c in _WFQ_ORDER)
    return {c: config.weight(c) / total for c in _WFQ_ORDER}


__all__ = ["WFQAllocation", "allocate_slots", "fair_share_fractions"]
