"""Hardened provider resilience gateway (round-2, additive to round-1).

A composable resilience stack that wraps the shared round-1
:class:`~app.providers.base.ProviderClient` *without editing it*:

* :mod:`.backoff` — full / equal / decorrelated jitter schedules + Retry-After.
* :mod:`.ratelimit` — an AIMD adaptive token bucket that backs off on 429s.
* :mod:`.breakers` — a per-model circuit-breaker registry with half-open probing.
* :mod:`.cache` — a request-hash-keyed TTL+LRU response cache with single-flight
  in-flight dedup (§12.3).
* :mod:`.hedging` — staggered duplicate requests for tail-latency cuts (idempotent
  ops only — a Wan render is never hedged).
* :mod:`.metering` — a fan-out usage sink with per-model rollups (§11.1 / §12.5).
* :mod:`.registry` — a multi-cloud provider abstraction with capability negotiation.
* :mod:`.gateway` — :class:`ResilientGateway`, which composes all of the above.
* :mod:`.chaos` — deterministic fault-injection primitives for the test suite.

Nothing here calls a provider or reads settings on import; the gateway is opt-in
and constructed by the (additive) ``providers.__init__`` seam.
"""

from __future__ import annotations

from .backoff import BackoffPolicy, BackoffSchedule, JitterStrategy
from .breakers import (
    BreakerConfig,
    BreakerRegistry,
    BreakerSnapshot,
    BreakerState,
    ModelBreaker,
)
from .cache import CacheConfig, CacheStats, ResponseCache, request_hash
from .chaos import (
    ChaosTransport,
    ChaoticAttempt,
    FakeClock,
    FaultKind,
    FaultPlan,
    FaultProfile,
    make_async_sleep,
)
from .degradation import (
    BudgetWindow,
    DegradationAdvice,
    DegradationAdvisor,
    DegradationLevel,
)
from .facade import GatewayCallable, GatewayChatProvider
from .factory import (
    adaptive_bucket_from_settings,
    build_gateway,
    gateway_config_from_settings,
    gateway_serves,
    registry_from_settings,
)
from .gateway import GatewayCall, GatewayConfig, ResilientGateway
from .hedging import HedgedExecutor, HedgePolicy, HedgeStats
from .metering import MeteringSink, MeterRollup, MeterSnapshot
from .ratelimit import AdaptiveRateConfig, AdaptiveTokenBucket
from .registry import (
    Capability,
    CapabilityUnavailable,
    Cloud,
    NegotiationResult,
    ProviderDescriptor,
    ProviderRegistry,
    dashscope_descriptor,
    openai_descriptor,
)
from .stats import GatewayCallStats, GatewaySnapshot

__all__ = [
    "AdaptiveRateConfig",
    "AdaptiveTokenBucket",
    "BackoffPolicy",
    "BackoffSchedule",
    "BreakerConfig",
    "BreakerRegistry",
    "BreakerSnapshot",
    "BreakerState",
    "BudgetWindow",
    "CacheConfig",
    "CacheStats",
    "Capability",
    "CapabilityUnavailable",
    "ChaosTransport",
    "ChaoticAttempt",
    "Cloud",
    "DegradationAdvice",
    "DegradationAdvisor",
    "DegradationLevel",
    "FakeClock",
    "FaultKind",
    "FaultPlan",
    "FaultProfile",
    "GatewayCall",
    "GatewayCallable",
    "GatewayCallStats",
    "GatewayChatProvider",
    "GatewayConfig",
    "GatewaySnapshot",
    "HedgePolicy",
    "HedgeStats",
    "HedgedExecutor",
    "JitterStrategy",
    "MeterRollup",
    "MeterSnapshot",
    "MeteringSink",
    "ModelBreaker",
    "NegotiationResult",
    "ProviderDescriptor",
    "ProviderRegistry",
    "ResilientGateway",
    "ResponseCache",
    "adaptive_bucket_from_settings",
    "build_gateway",
    "dashscope_descriptor",
    "gateway_config_from_settings",
    "gateway_serves",
    "make_async_sleep",
    "openai_descriptor",
    "registry_from_settings",
    "request_hash",
]
