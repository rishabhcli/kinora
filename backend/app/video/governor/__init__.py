"""Provider SLA / quota **governor** — governance above the round-1 video router.

The round-1 :class:`~app.providers.video_router.VideoRouter` does health-based
*failover* between video backends. This package is the governance layer above it:
it decides whether a provider *should* be asked for another render at all, paces
submissions under each provider's published limits, tracks each provider against
its SLOs, and shares scarce provider capacity fairly across concurrent
books/sessions — emitting breach/alert events the operator layer consumes.

Pieces (each pure, clock-injected, deterministic under a fake clock):

* :mod:`.clock` — the injectable monotonic :data:`Clock` + a :class:`FakeClock`.
* :mod:`.store` — the redis-shaped :class:`GovernorStore` counters account through
  (an :class:`InMemoryGovernorStore` backs tests); cross-process correct.
* :mod:`.quota` — per-provider, windowed :class:`QuotaAccountant`: requests/min,
  concurrent jobs, daily video-seconds, monthly spend (§11.1).
* :mod:`.sla` — :class:`SlaTracker`: observed-vs-target success/latency, error-budget
  burn, and a coarse A–F :class:`SlaGrade`.
* :mod:`.throttle` — :class:`ProviderThrottle`: a 429/Retry-After-aware submission
  pacer that backs off on observed rate-limit responses and eases back on recovery.
* :mod:`.fairshare` — :class:`FairShareAllocator`: weighted, anti-starvation sharing
  of scarce capacity across tenants so one big book can't monopolise a provider.
* :mod:`.oracle` — :class:`CapacityOracle`: the read-only "can provider X take a job
  now? when free?" surface the router/scheduler queries.
* :mod:`.events` — typed SLA/quota breach + alert :class:`GovernorEvent` s and a fan-
  out :class:`GovernorEventBus`.
* :mod:`.governor` — :class:`ProviderGovernor`, the single object composing all of
  the above with an admit → complete render lease lifecycle.
* :mod:`.config` — pure-data per-provider :class:`ProviderProfile` / :class:`GovernorConfig`.

Nothing here calls a provider, reads settings on import, or touches
``KINORA_LIVE_VIDEO``. The governor only *decides and records*; the router still
performs the render. The composition root translates ``Settings`` into a
:class:`GovernorConfig` and injects a real Redis-backed :class:`GovernorStore`.
"""

from __future__ import annotations

from .clock import Clock, FakeClock, monotonic
from .config import GovernorConfig, ProviderProfile, default_video_profiles
from .events import (
    EventCode,
    EventSink,
    GovernorEvent,
    GovernorEventBus,
    Severity,
)
from .fairshare import FairShareAllocator, FairShareConfig, FairShareDecision
from .governor import ProviderGovernor, RenderLease
from .oracle import CapacityOracle, CapacityVerdict, DenyReason, best_provider
from .quota import (
    DimensionUsage,
    QuotaAccountant,
    QuotaDecision,
    QuotaDimension,
    QuotaLimits,
    QuotaRegistry,
    RenderCost,
    window_start,
)
from .sla import SlaGrade, SlaObjective, SlaSnapshot, SlaTracker
from .store import GovernorStore, InMemoryGovernorStore
from .throttle import ProviderThrottle, ThrottleConfig, ThrottleState

__all__ = [
    # clock
    "Clock",
    "FakeClock",
    "monotonic",
    # store
    "GovernorStore",
    "InMemoryGovernorStore",
    # quota
    "DimensionUsage",
    "QuotaAccountant",
    "QuotaDecision",
    "QuotaDimension",
    "QuotaLimits",
    "QuotaRegistry",
    "RenderCost",
    "window_start",
    # sla
    "SlaGrade",
    "SlaObjective",
    "SlaSnapshot",
    "SlaTracker",
    # throttle
    "ProviderThrottle",
    "ThrottleConfig",
    "ThrottleState",
    # fairshare
    "FairShareAllocator",
    "FairShareConfig",
    "FairShareDecision",
    # oracle
    "CapacityOracle",
    "CapacityVerdict",
    "DenyReason",
    "best_provider",
    # events
    "EventCode",
    "EventSink",
    "GovernorEvent",
    "GovernorEventBus",
    "Severity",
    # config
    "GovernorConfig",
    "ProviderProfile",
    "default_video_profiles",
    # governor
    "ProviderGovernor",
    "RenderLease",
]
