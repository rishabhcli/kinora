"""QoS fabric: backpressure + priority scheduling for render work (kinora.md §4.9/§12.2).

A pure-policy layer that turns the render queue's committed/speculative/cold lanes
into **QoS classes** and arbitrates them under load — strict-priority for the
near-reader committed zone, weighted-fair-queuing so cold never fully starves,
EDF-style deadline urgency for soon-needed shots, aging/anti-starvation, admission
control + backpressure, least-value-first load shedding, and per-tenant/per-book
fairness. Everything is pure policy over an injectable backlog + virtual clock
(:mod:`app.qos.clock`), so it is fully deterministic and unit-testable with no
infra, no network, and never spends video-seconds.

The fabric is **additive**: it never rewrites :mod:`app.scheduler` or
:mod:`app.queue`. :mod:`app.qos.adapter` is the only seam onto the live
``QueuedJob`` / ``RenderPriority`` queue.

Modules:

* :mod:`app.qos.model` — :class:`QoSClass` + :class:`QoSItem`, the schedulable unit.
* :mod:`app.qos.config` — :class:`QoSConfig`, every tunable policy knob (pydantic v2).
* :mod:`app.qos.clock` — :class:`VirtualClock` / :class:`WallClock`.
* :mod:`app.qos.admission` — admit / defer / reject + backpressure (§12.2).
* :mod:`app.qos.aging` — promote long-waiting cold/speculative work.
* :mod:`app.qos.deadline` — EDF urgency within a class (§4.3/§4.6).
* :mod:`app.qos.wfq` — committed reservation + weighted-fair deficit round robin.
* :mod:`app.qos.fairness` — max-min fair per-book split (§12.2).
* :mod:`app.qos.shedding` — least-value-first load shedding (§12.2/§12.4).
* :mod:`app.qos.scheduler` — :class:`QoSScheduler`, the composed dispatch engine.
* :mod:`app.qos.adapter` — lift live ``QueuedJob`` onto :class:`QoSItem`.
* :mod:`app.qos.load` — a seeded synthetic-load generator for tests/sims.
"""

from __future__ import annotations

from app.qos.adapter import job_to_qos_item, jobs_to_qos_items
from app.qos.admission import AdmissionPolicy, AdmissionVerdict
from app.qos.aging import aging_boost, effective_class_value, is_starving
from app.qos.clock import Clock, VirtualClock, WallClock
from app.qos.config import QoSConfig
from app.qos.deadline import edf_key, is_expired, is_urgent, urgency_score
from app.qos.fairness import (
    fair_book_allocation,
    max_min_fair_shares,
    starvation_free,
)
from app.qos.load import LoadGen
from app.qos.model import QoSClass, QoSItem, SheddingReason
from app.qos.scheduler import DispatchResult, QoSScheduler
from app.qos.shedding import ShedVictim, select_victims, shed
from app.qos.wfq import WFQAllocation, allocate_slots, fair_share_fractions

__all__ = [
    "AdmissionPolicy",
    "AdmissionVerdict",
    "Clock",
    "DispatchResult",
    "LoadGen",
    "QoSClass",
    "QoSConfig",
    "QoSItem",
    "QoSScheduler",
    "ShedVictim",
    "SheddingReason",
    "VirtualClock",
    "WFQAllocation",
    "WallClock",
    "aging_boost",
    "allocate_slots",
    "edf_key",
    "effective_class_value",
    "fair_book_allocation",
    "fair_share_fractions",
    "is_expired",
    "is_starving",
    "is_urgent",
    "job_to_qos_item",
    "jobs_to_qos_items",
    "max_min_fair_shares",
    "select_victims",
    "shed",
    "starvation_free",
    "urgency_score",
]
