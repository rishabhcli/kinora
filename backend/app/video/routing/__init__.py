"""Multi-provider video router **v2** — a drop-in :class:`VideoBackend`.

A production router layered over N :class:`~app.providers.video_router.VideoBackend`
transports that adds, over the round-1 ``VideoRouter``:

* per-provider **health tracking** (rolling success-rate, p50/p95 latency, an
  error-class histogram) + **circuit breakers** with exponential cooldown
  (:mod:`.health`),
* a pluggable **selection-policy** interface with concrete policies —
  cheapest-capable, fastest, highest-quality, weighted-blend — all
  capability-filtered to the requested mode (:mod:`.policy`, :mod:`.capabilities`),
* **hedged / racing** requests with a budget guard (:mod:`.router`),
* **sticky routing** (shot family → same provider for visual continuity)
  (:mod:`.sticky`),
* per-provider **concurrency limits + token-bucket rate limits**
  (:mod:`.concurrency`),
* automatic **failover** across the remaining healthy providers, and
* full structured-log + **metrics** of every routing decision (:mod:`.metrics`).

The :class:`RoutingVideoRouter` implements the ``name`` / ``render`` / ``healthy``
contract the Generator/pipeline already depend on, so it drops in unchanged. The
``LiveVideoDisabled`` spend gate is propagated unchanged and is never a health
fault — exactly as the round-1 router guarantees.
"""

from __future__ import annotations

from .capabilities import (
    ALL_MODES,
    NEUTRAL_PROFILE,
    ProfileBook,
    ProviderProfile,
    filter_capable,
    normalize_profiles,
)
from .concurrency import (
    GateBook,
    GateConfig,
    ProviderGate,
    TokenBucket,
)
from .factory import build_routing_router, infer_profile
from .health import (
    CircuitState,
    ErrorClass,
    HealthConfig,
    HealthSnapshot,
    ProviderHealth,
    classify_error,
)
from .metrics import RouteDecision, RouterMetrics, emit_decision
from .policy import (
    CapabilityFilteredPolicy,
    CheapestCapablePolicy,
    FastestPolicy,
    HealthView,
    HighestQualityPolicy,
    PolicyKind,
    RouteContext,
    SelectionPolicy,
    WeightedBlendPolicy,
    build_policy,
)
from .router import RouterV2Policy, RoutingVideoRouter
from .sticky import StickyStore, apply_stickiness, family_key

__all__ = [
    "ALL_MODES",
    "NEUTRAL_PROFILE",
    "CapabilityFilteredPolicy",
    "CheapestCapablePolicy",
    "CircuitState",
    "ErrorClass",
    "FastestPolicy",
    "GateBook",
    "GateConfig",
    "HealthConfig",
    "HealthSnapshot",
    "HealthView",
    "HighestQualityPolicy",
    "PolicyKind",
    "ProfileBook",
    "ProviderGate",
    "ProviderHealth",
    "ProviderProfile",
    "RouteContext",
    "RouteDecision",
    "RouterMetrics",
    "RouterV2Policy",
    "RoutingVideoRouter",
    "SelectionPolicy",
    "StickyStore",
    "TokenBucket",
    "WeightedBlendPolicy",
    "apply_stickiness",
    "build_policy",
    "build_routing_router",
    "classify_error",
    "emit_decision",
    "family_key",
    "filter_capable",
    "infer_profile",
    "normalize_profiles",
]
