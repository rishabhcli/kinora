"""Per-provider concurrency limits + token-bucket rate limiting.

Each backend a video render can land on has its own throughput envelope: a hosted
Wan id tolerates only so many concurrent async tasks before the provider queues
or throttles, and the DashScope-intl tier rate-limits independently per model. The
v2 router gives every backend its own :class:`ProviderGate` — a counting semaphore
(concurrency cap) wrapped around a :class:`TokenBucket` (sustained rate + burst) —
so one hot backend can't be hammered past its envelope while siblings stay idle.

Both primitives are async and time-injected (a monotonic ``clock`` + an async
``sleep``) so tests exercise saturation and refill deterministically without real
waits. The token bucket mirrors the semantics of
:class:`app.providers.base.TokenBucket` but is owned here so the router's gating
is self-contained and independently testable.

A gate is acquired via ``async with gate.slot():`` which first waits for a rate
token, then a concurrency slot; the slot is released on exit, and the rate token
is consumed (not returned) so the bucket models sustained throughput correctly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

#: Injectable monotonic clock (seconds). Tests pass a controllable fake.
Clock = Callable[[], float]
#: Injectable async sleep (so tests advance a fake clock instead of waiting).
AsyncSleep = Callable[[float], Awaitable[None]]

#: Absorbs floating-point dust so a refill landing a hair below the request never
#: traps :meth:`TokenBucket.acquire` (same trick as the resilience limiter).
_TOKEN_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Throughput envelope for one backend's :class:`ProviderGate`.

    Attributes:
        max_concurrency: Max in-flight renders to this backend (>=1).
        rate_per_s: Sustained renders-per-second the token bucket refills at.
            ``0`` disables rate limiting (concurrency cap only).
        burst: Token-bucket capacity (instantaneous burst). Defaults to
            ``max_concurrency`` when omitted/zero.
    """

    max_concurrency: int = 4
    rate_per_s: float = 0.0
    burst: int = 0

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.rate_per_s < 0:
            raise ValueError("rate_per_s must be >= 0")
        if self.burst < 0:
            raise ValueError("burst must be >= 0")


class TokenBucket:
    """An async token bucket: continuous refill at ``rate``, capped at ``burst``."""

    def __init__(
        self,
        *,
        rate_per_s: float,
        burst: int,
        clock: Clock = time.monotonic,
        sleep: AsyncSleep | None = None,
    ) -> None:
        self._rate = float(rate_per_s)
        self._capacity = float(max(burst, 1))
        self._tokens = self._capacity
        self._clock = clock
        self._sleep: AsyncSleep = sleep or asyncio.sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def available_tokens(self) -> float:
        """Non-refilled token count (inspection only; not a guarantee)."""
        return self._tokens

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(now - self._updated, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available. A no-op when ``rate_per_s`` is 0."""
        if self._rate <= 0:
            return
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens + _TOKEN_EPSILON >= tokens:
                    self._tokens = max(0.0, self._tokens - tokens)
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate
            await self._sleep(wait_s)


class ProviderGate:
    """A backend's throughput gate: a rate token then a concurrency slot.

    Acquire with ``async with gate.slot(): ...`` — it waits for a rate token,
    then occupies one of ``max_concurrency`` slots for the duration of the block,
    releasing the slot on exit. The rate token is consumed (sustained-rate model).
    """

    def __init__(
        self,
        config: GateConfig | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: AsyncSleep | None = None,
    ) -> None:
        self.config = config or GateConfig()
        self._sem = asyncio.Semaphore(self.config.max_concurrency)
        burst = self.config.burst or self.config.max_concurrency
        self._bucket = TokenBucket(
            rate_per_s=self.config.rate_per_s,
            burst=burst,
            clock=clock,
            sleep=sleep,
        )
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """Renders currently holding a concurrency slot on this backend."""
        return self._in_flight

    @property
    def available_tokens(self) -> float:
        return self._bucket.available_tokens

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire a rate token + a concurrency slot for the duration of the block."""
        await self._bucket.acquire()
        await self._sem.acquire()
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._sem.release()


class GateBook:
    """A name → :class:`ProviderGate` registry (one gate per backend).

    Backends without a configured :class:`GateConfig` get a permissive default
    gate (its ``max_concurrency`` from ``default``) so an unconfigured router still
    bounds fan-out sanely.
    """

    def __init__(
        self,
        configs: dict[str, GateConfig] | None = None,
        *,
        default: GateConfig | None = None,
        clock: Clock = time.monotonic,
        sleep: AsyncSleep | None = None,
    ) -> None:
        self._default = default or GateConfig()
        self._gates: dict[str, ProviderGate] = {}
        for name, cfg in (configs or {}).items():
            self._gates[name] = ProviderGate(cfg, clock=clock, sleep=sleep)
        self._clock = clock
        self._sleep = sleep

    def gate(self, name: str) -> ProviderGate:
        """The gate for backend ``name`` (lazily created from the default)."""
        gate = self._gates.get(name)
        if gate is None:
            gate = ProviderGate(self._default, clock=self._clock, sleep=self._sleep)
            self._gates[name] = gate
        return gate


__all__ = [
    "AsyncSleep",
    "Clock",
    "GateBook",
    "GateConfig",
    "ProviderGate",
    "TokenBucket",
]
