"""A small chaos-injection harness — probabilistic faults/latency, OFF in prod.

Resilience you can't *exercise* is resilience you don't have. This harness lets a
test (or a deliberately-enabled local soak run) inject failures and latency into a
call site so the retry / breaker / bulkhead policies wrapped around it are proven
against real faults, not just inspected.

Two production-safety guarantees, both load-bearing:

1. **Disabled by default.** A :class:`ChaosMonkey` with ``enabled=False`` (the
   default) is a pure passthrough — :meth:`before_call` does nothing. The only ways
   to arm it are constructing one with ``enabled=True`` (tests do this directly) or
   :func:`chaos_from_settings`, which **refuses to arm outside ``local``** — even
   if the env var is set — so a stray ``RESILIENCE_CHAOS_ENABLED=true`` in a prod
   deployment is a no-op.
2. **Deterministic when seeded.** Pass a seeded ``random.Random`` and the injected
   sequence is fixed; combined with a :class:`~app.resilience.clock.ManualClock`,
   chaos-driven tests are reproducible and instant.

The harness raises faults from the resilience taxonomy
(:class:`~app.resilience.errors.ChaosInjectedError` for the transient family,
plus throttle / timeout / permanent variants) so policies treat an injected fault
exactly like a real one. Latency is "slept" through the injected clock, so a
latency fault advances virtual time rather than blocking.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger

from .clock import SYSTEM_CLOCK, Clock
from .errors import (
    AuthError,
    CallTimeout,
    ChaosInjectedError,
    PermanentError,
    RateLimitedError,
)

logger = get_logger("app.resilience.chaos")


class ChaosFault(StrEnum):
    """The kinds of fault the monkey can inject."""

    NONE = "none"
    TRANSIENT = "transient"  # ChaosInjectedError (retryable)
    TIMEOUT = "timeout"  # CallTimeout (retryable)
    THROTTLE = "throttle"  # RateLimitedError (retryable, carries retry_after)
    PERMANENT = "permanent"  # PermanentError (NOT retryable)
    AUTH = "auth"  # AuthError (NOT retryable)


@dataclass(frozen=True, slots=True)
class ChaosConfig:
    """Probabilities + latency bounds for a :class:`ChaosMonkey` (sum of fault
    probabilities must be <= 1; the remainder is "call proceeds normally")."""

    fault_probability: float = 0.0
    #: Distribution of *which* fault fires given that one fires. Defaults to all
    #: transient. Keys omitted get zero weight.
    fault_weights: dict[ChaosFault, float] = field(
        default_factory=lambda: {ChaosFault.TRANSIENT: 1.0}
    )
    #: Probability of injecting extra latency (independent of a fault).
    latency_probability: float = 0.0
    latency_min_s: float = 0.0
    latency_max_s: float = 0.0
    #: ``retry_after_s`` attached to an injected THROTTLE.
    throttle_retry_after_s: float = 1.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.fault_probability <= 1.0):
            raise ValueError("fault_probability must be in [0, 1]")
        if not (0.0 <= self.latency_probability <= 1.0):
            raise ValueError("latency_probability must be in [0, 1]")
        if self.latency_max_s < self.latency_min_s:
            raise ValueError("latency_max_s must be >= latency_min_s")
        if self.latency_min_s < 0:
            raise ValueError("latency_min_s must be >= 0")
        if any(w < 0 for w in self.fault_weights.values()):
            raise ValueError("fault weights must be >= 0")


class ChaosMonkey:
    """Injects faults/latency at a call site — only when explicitly armed.

    Construct ``enabled=True`` with a seeded ``rng`` for deterministic tests. In
    production wire it via :func:`chaos_from_settings`, which never arms outside
    ``local``.
    """

    def __init__(
        self,
        name: str = "chaos",
        config: ChaosConfig | None = None,
        *,
        enabled: bool = False,
        rng: random.Random | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.name = name
        self.config = config or ChaosConfig()
        self.enabled = enabled
        self._rng = rng or random.Random()
        self._clock = clock
        self._faults_injected = 0
        self._latencies_injected = 0

    @property
    def faults_injected(self) -> int:
        return self._faults_injected

    @property
    def latencies_injected(self) -> int:
        return self._latencies_injected

    async def before_call(self) -> None:
        """Maybe inject latency and/or a fault before the wrapped call runs.

        A no-op when ``enabled`` is False. When armed, rolls latency first (so a
        slow-then-fail fault models a realistic timeout), then a fault.
        """
        if not self.enabled:
            return
        await self._maybe_latency()
        self._maybe_fault()

    async def _maybe_latency(self) -> None:
        cfg = self.config
        if cfg.latency_probability <= 0.0:
            return
        if self._rng.random() < cfg.latency_probability:
            delay = self._rng.uniform(cfg.latency_min_s, cfg.latency_max_s)
            self._latencies_injected += 1
            logger.debug("resilience.chaos.latency", monkey=self.name, delay_s=delay)
            await self._clock.sleep(delay)

    def _maybe_fault(self) -> None:
        cfg = self.config
        if cfg.fault_probability <= 0.0:
            return
        if self._rng.random() >= cfg.fault_probability:
            return
        fault = self._pick_fault()
        self._faults_injected += 1
        logger.warning("resilience.chaos.fault", monkey=self.name, fault=fault.value)
        raise _fault_exception(fault, cfg.throttle_retry_after_s)

    def _pick_fault(self) -> ChaosFault:
        weights = {k: v for k, v in self.config.fault_weights.items() if v > 0}
        if not weights:
            return ChaosFault.TRANSIENT
        total = sum(weights.values())
        roll = self._rng.random() * total
        cumulative = 0.0
        for fault, weight in weights.items():
            cumulative += weight
            if roll < cumulative:
                return fault
        return next(iter(weights))


def _fault_exception(fault: ChaosFault, throttle_retry_after_s: float) -> Exception:
    if fault is ChaosFault.TIMEOUT:
        return CallTimeout("chaos: injected timeout")
    if fault is ChaosFault.THROTTLE:
        return RateLimitedError(
            "chaos: injected throttle", retry_after_s=throttle_retry_after_s
        )
    if fault is ChaosFault.PERMANENT:
        return PermanentError("chaos: injected permanent fault")
    if fault is ChaosFault.AUTH:
        return AuthError("chaos: injected auth failure")
    return ChaosInjectedError("chaos: injected transient fault")


def chaos_from_settings(name: str = "chaos") -> ChaosMonkey:
    """Build a :class:`ChaosMonkey` from settings — **never armed outside local**.

    Reads ``resilience_chaos_*`` from :class:`~app.core.config.Settings`. The monkey
    is armed only when ``resilience_chaos_enabled`` is True **and** the app is
    running in the ``local`` environment; in any other environment it is forced
    disabled regardless of the flag, so chaos can never leak into production.
    """
    from app.core.config import get_settings

    settings = get_settings()
    armed = bool(getattr(settings, "resilience_chaos_enabled", False)) and settings.is_local
    if getattr(settings, "resilience_chaos_enabled", False) and not settings.is_local:
        logger.warning(
            "resilience.chaos.refused_non_local",
            app_env=settings.app_env,
            note="chaos injection is disabled outside the local environment",
        )
    config = ChaosConfig(
        fault_probability=float(getattr(settings, "resilience_chaos_fault_probability", 0.0)),
        latency_probability=float(
            getattr(settings, "resilience_chaos_latency_probability", 0.0)
        ),
        latency_min_s=float(getattr(settings, "resilience_chaos_latency_min_s", 0.0)),
        latency_max_s=float(getattr(settings, "resilience_chaos_latency_max_s", 0.0)),
    )
    return ChaosMonkey(name, config, enabled=armed)


__all__ = [
    "ChaosConfig",
    "ChaosFault",
    "ChaosMonkey",
    "chaos_from_settings",
]
