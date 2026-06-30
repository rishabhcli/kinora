"""A circuit breaker — closed / open / half-open, rate *and* consecutive triggers.

Sitting in front of a flaky dependency, the breaker stops hammering something that
is already down: after enough failures it *opens* and rejects calls instantly
(:class:`~app.resilience.errors.CircuitOpen`) for a cooldown, then *half-opens* to
admit a limited number of probe calls; a probe success closes it, a probe failure
re-opens it. That converts a slow cascading outage into a fast, bounded one and
gives the dependency room to recover.

This breaker trips on **either** signal, whichever fires first:

* **consecutive failures** — N failures in a row (great for a hard outage); and
* **failure rate** — failures / total over a rolling window of the last
  ``window_size`` outcomes, once at least ``min_calls`` have been seen (great for a
  partial brownout where successes are interleaved).

The half-open state admits up to ``half_open_max_calls`` concurrent probes and
needs ``half_open_success_threshold`` of them to succeed before closing. All
transitions are driven by an injected monotonic clock and guarded by an
:class:`asyncio.Lock`, so state-machine tests are exhaustive and instant.

This generalizes the per-model provider breaker (which keyed by model and only had
the consecutive trigger) to a single named breaker any dependency can adopt;
[app.resilience.registry][] keeps a family of these keyed by dependency name.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import get_logger

from .clock import SYSTEM_CLOCK, Clock
from .errors import CircuitOpen

logger = get_logger("app.resilience.breaker")


class BreakerState(StrEnum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """Tunables for a :class:`CircuitBreaker` (deterministic; no env reads)."""

    #: Trip after this many failures in a row (0 disables the consecutive trigger).
    consecutive_failure_threshold: int = 5
    #: Trip when the rolling failure *rate* >= this (0..1), once ``min_calls`` seen
    #: (>= 1.0 effectively disables the rate trigger).
    failure_rate_threshold: float = 0.5
    #: Size of the rolling outcome window the rate is computed over.
    window_size: int = 20
    #: Don't evaluate the rate trigger until at least this many outcomes are in the
    #: window (avoids tripping on a single early failure).
    min_calls: int = 5
    #: How long OPEN waits before allowing HALF_OPEN probes (seconds).
    cooldown_s: float = 20.0
    #: How many probe calls HALF_OPEN admits concurrently before deciding.
    half_open_max_calls: int = 1
    #: Probe successes required (within the probe window) to close the breaker.
    half_open_success_threshold: int = 1

    def __post_init__(self) -> None:
        if self.consecutive_failure_threshold < 0:
            raise ValueError("consecutive_failure_threshold must be >= 0")
        if not (0.0 <= self.failure_rate_threshold <= 1.0):
            raise ValueError("failure_rate_threshold must be in [0, 1]")
        if self.window_size < 1:
            raise ValueError("window_size must be >= 1")
        if self.min_calls < 1:
            raise ValueError("min_calls must be >= 1")
        if self.cooldown_s < 0:
            raise ValueError("cooldown_s must be >= 0")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if self.half_open_success_threshold < 1:
            raise ValueError("half_open_success_threshold must be >= 1")
        if self.half_open_success_threshold > self.half_open_max_calls:
            raise ValueError("half_open_success_threshold cannot exceed half_open_max_calls")
        if self.consecutive_failure_threshold == 0 and self.failure_rate_threshold >= 1.0:
            raise ValueError("at least one trip trigger must be active")


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    """An immutable view of a breaker's state (telemetry + tests)."""

    name: str
    state: BreakerState
    consecutive_failures: int
    window_failures: int
    window_total: int
    total_successes: int
    total_failures: int
    total_rejections: int
    opened_count: int


class CircuitBreaker:
    """A single named breaker. See module docstring for the trip semantics.

    Concurrency: :meth:`before_call` reserves a probe slot in HALF_OPEN under the
    lock so at most ``half_open_max_calls`` probes run at once; :meth:`record_success`
    / :meth:`record_failure` release and score them. All transitions are clock-driven
    and lock-guarded.
    """

    def __init__(
        self,
        name: str,
        config: BreakerConfig | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._window: deque[bool] = deque(maxlen=self.config.window_size)  # True = failure
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self._half_open_successes = 0
        self._total_successes = 0
        self._total_failures = 0
        self._total_rejections = 0
        self._opened_count = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            name=self.name,
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            window_failures=sum(self._window),
            window_total=len(self._window),
            total_successes=self._total_successes,
            total_failures=self._total_failures,
            total_rejections=self._total_rejections,
            opened_count=self._opened_count,
        )

    async def before_call(self) -> None:
        """Gate a call. Raises :class:`CircuitOpen` when the breaker rejects.

        Transitions OPEN → HALF_OPEN once the cooldown elapses, then admits up to
        ``half_open_max_calls`` concurrent probes; further probes are rejected until
        the in-flight ones resolve.
        """
        async with self._lock:
            if self._state is BreakerState.OPEN:
                if self._clock.monotonic() - self._opened_at >= self.config.cooldown_s:
                    self._enter_half_open()
                else:
                    self._total_rejections += 1
                    raise CircuitOpen(
                        f"circuit {self.name!r} open; rejecting without attempting",
                        name=self.name,
                    )
            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_inflight >= self.config.half_open_max_calls:
                    self._total_rejections += 1
                    raise CircuitOpen(
                        f"circuit {self.name!r} half-open; probe budget exhausted",
                        name=self.name,
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
            self._window.append(False)

    async def record_failure(self) -> None:
        async with self._lock:
            self._total_failures += 1
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._open()
                return
            self._consecutive_failures += 1
            self._window.append(True)
            if self._should_trip():
                self._open()

    def _should_trip(self) -> bool:
        cfg = self.config
        if (
            cfg.consecutive_failure_threshold > 0
            and self._consecutive_failures >= cfg.consecutive_failure_threshold
        ):
            return True
        if cfg.failure_rate_threshold < 1.0 and len(self._window) >= cfg.min_calls:
            rate = sum(self._window) / len(self._window)
            if rate >= cfg.failure_rate_threshold:
                return True
        return False

    # -- transitions (lock held) ----------------------------------------- #

    def _open(self) -> None:
        if self._state is not BreakerState.OPEN:
            self._opened_count += 1
            logger.warning("resilience.breaker.open", breaker=self.name)
        self._state = BreakerState.OPEN
        self._opened_at = self._clock.monotonic()
        self._half_open_inflight = 0
        self._half_open_successes = 0

    def _enter_half_open(self) -> None:
        logger.info("resilience.breaker.half_open", breaker=self.name)
        self._state = BreakerState.HALF_OPEN
        self._half_open_inflight = 0
        self._half_open_successes = 0

    def _close(self) -> None:
        logger.info("resilience.breaker.close", breaker=self.name)
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._window.clear()
        self._half_open_inflight = 0
        self._half_open_successes = 0


__all__ = [
    "BreakerConfig",
    "BreakerSnapshot",
    "BreakerState",
    "CircuitBreaker",
]
