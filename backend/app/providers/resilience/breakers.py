"""Per-model circuit breakers with half-open probing.

The round-1 :class:`~app.providers.base.CircuitBreaker` is *one* breaker shared by
the whole client — so a flaky ``qwen-image-plus`` (which 429s on its own quota)
would trip the breaker for ``qwen3.7-max`` chat too. That cross-contamination is
exactly wrong: DashScope's quotas and outages are **per-model**.

:class:`BreakerRegistry` keeps an independent :class:`ModelBreaker` keyed by model
id, so one model going dark never starves the others. Each breaker is the classic
three-state machine with an explicit **half-open probe budget** (CLOSED → OPEN
after N consecutive failures → after a cooldown, HALF_OPEN admits up to
``half_open_max_calls`` probes → CLOSED on a probe success, back to OPEN on a probe
failure).

Pure logic, injectable monotonic clock, fully lock-guarded for concurrent probes —
exhaustively testable without sleeping. This mirrors the shape of the round-1
breaker and the router's :class:`~app.providers.video_router.BackendHealth`, but
generalized to a keyed family and a multi-probe half-open window.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import get_logger

from ..errors import CircuitOpenError

logger = get_logger("app.providers.resilience.breakers")

#: Injectable monotonic clock (seconds). Tests pass a controllable fake.
Clock = Callable[[], float]


class BreakerState(StrEnum):
    """The circuit state of one per-model breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """Tunables for a :class:`ModelBreaker` (deterministic; no env reads)."""

    failure_threshold: int = 5
    recovery_s: float = 20.0
    #: How many probe calls HALF_OPEN admits before deciding. >1 lets a couple of
    #: probes confirm recovery before fully closing, avoiding a flap on one fluke.
    half_open_max_calls: int = 1
    #: Probe successes required (within the probe window) to close the breaker.
    half_open_success_threshold: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if self.half_open_success_threshold < 1:
            raise ValueError("half_open_success_threshold must be >= 1")
        if self.half_open_success_threshold > self.half_open_max_calls:
            raise ValueError("half_open_success_threshold cannot exceed half_open_max_calls")


@dataclass
class BreakerSnapshot:
    """An immutable view of a breaker's state (telemetry + tests)."""

    model: str
    state: BreakerState
    consecutive_failures: int
    total_successes: int
    total_failures: int
    total_rejections: int


class ModelBreaker:
    """A three-state circuit breaker for a single model id.

    Concurrency: ``before_call`` reserves a probe slot in HALF_OPEN under the lock,
    so at most ``half_open_max_calls`` probes run at once; ``record_*`` release/score
    them. All transitions are clock-driven and lock-guarded.
    """

    def __init__(self, model: str, config: BreakerConfig, *, clock: Clock = time.monotonic) -> None:
        self.model = model
        self.config = config
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self._half_open_successes = 0
        self._half_open_failures = 0
        self._total_successes = 0
        self._total_failures = 0
        self._total_rejections = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            model=self.model,
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            total_successes=self._total_successes,
            total_failures=self._total_failures,
            total_rejections=self._total_rejections,
        )

    async def before_call(self) -> None:
        """Gate a call. Raises :class:`CircuitOpenError` when the breaker rejects.

        Transitions OPEN → HALF_OPEN once the cooldown elapses, then admits up to
        ``half_open_max_calls`` concurrent probes; further probes are rejected
        until the in-flight ones resolve.
        """
        async with self._lock:
            if self._state is BreakerState.OPEN:
                if self._clock() - self._opened_at >= self.config.recovery_s:
                    self._enter_half_open()
                else:
                    self._total_rejections += 1
                    raise CircuitOpenError(
                        f"circuit open for model {self.model!r}; rejecting without attempting",
                        code="CircuitOpen",
                    )
            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_inflight >= self.config.half_open_max_calls:
                    self._total_rejections += 1
                    raise CircuitOpenError(
                        f"circuit half-open for model {self.model!r}; probe budget exhausted",
                        code="CircuitHalfOpenBusy",
                    )
                self._half_open_inflight += 1

    async def record_success(self) -> None:
        async with self._lock:
            self._total_successes += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_success_threshold:
                    self._close()
                return
            self._consecutive_failures = 0
            self._state = BreakerState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._half_open_failures += 1
                self._open()
                return
            if self._consecutive_failures >= self.config.failure_threshold:
                self._open()

    # -- transitions (lock held) ----------------------------------------- #

    def _open(self) -> None:
        if self._state is not BreakerState.OPEN:
            logger.warning("breaker.open", model=self.model)
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_inflight = 0
        self._half_open_successes = 0
        self._half_open_failures = 0

    def _enter_half_open(self) -> None:
        logger.info("breaker.half_open", model=self.model)
        self._state = BreakerState.HALF_OPEN
        self._half_open_inflight = 0
        self._half_open_successes = 0
        self._half_open_failures = 0

    def _close(self) -> None:
        logger.info("breaker.close", model=self.model)
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._half_open_inflight = 0
        self._half_open_successes = 0
        self._half_open_failures = 0


class BreakerRegistry:
    """A family of per-model :class:`ModelBreaker` s, created on demand.

    Keyed by model id; the first call for a model lazily creates its breaker with
    the registry's default config (or a per-model override). Thread/async-safe
    creation via a registry-level lock; each breaker then has its own lock.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        clock: Clock = time.monotonic,
        overrides: dict[str, BreakerConfig] | None = None,
    ) -> None:
        self._default = config or BreakerConfig()
        self._clock = clock
        self._overrides = dict(overrides or {})
        self._breakers: dict[str, ModelBreaker] = {}
        self._lock = asyncio.Lock()

    async def get(self, model: str) -> ModelBreaker:
        """Return (creating if needed) the breaker for ``model``."""
        existing = self._breakers.get(model)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._breakers.get(model)
            if existing is None:
                cfg = self._overrides.get(model, self._default)
                existing = ModelBreaker(model, cfg, clock=self._clock)
                self._breakers[model] = existing
            return existing

    def peek(self, model: str) -> ModelBreaker | None:
        """Return the breaker for ``model`` if it exists, without creating one."""
        return self._breakers.get(model)

    def snapshots(self) -> list[BreakerSnapshot]:
        """A snapshot of every known breaker (telemetry + tests)."""
        return [b.snapshot() for b in self._breakers.values()]

    def models(self) -> list[str]:
        return list(self._breakers.keys())


__all__ = [
    "BreakerConfig",
    "BreakerRegistry",
    "BreakerSnapshot",
    "BreakerState",
    "Clock",
    "ModelBreaker",
]
