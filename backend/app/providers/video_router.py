"""Multi-provider video routing — health-based failover + optional racing (§9.2).

A Wan-class render can come from more than one place: the default hosted DashScope
id, a quality-override id, a future second hosted region, or the §12.6 Alibaba
``VideoSynthesis`` worker. This module puts a thin, deterministic *router* in front
of one or more :class:`VideoBackend` s so the Generator can keep calling a single
``render(WanSpec)`` while gaining failover and (opt-in) racing underneath.

Design rules honoured here:

* **The spend gate is sacred.** ``LiveVideoDisabled`` is propagated **unchanged** and
  is *never* counted as a backend health failure — it is a deliberate spend gate, not
  a fault. With ``KINORA_LIVE_VIDEO`` off, every backend's ``render`` raises it and the
  router re-raises the first one, having submitted nothing.
* **Non-retryable errors short-circuit.** A 4xx ``ProviderBadRequest`` means the
  *spec* is wrong; trying a second backend would just fail the same way, so the router
  raises immediately. Only *retryable* transport faults (``retryable=True``) advance to
  the next backend.
* **Health is pure logic.** :class:`BackendHealth` is a small circuit-breaker driven by
  an injectable monotonic clock (consecutive failures → ``OPEN`` for a cooldown →
  ``HALF_OPEN`` probe → ``CLOSED``). No wall-clock, no RNG — exhaustively testable.

The router itself is *not* a provider transport; it composes the real
:class:`~app.providers.video.VideoProvider` instances (each its own DashScope client),
so it inherits their retries/breaker/rate-limit without re-implementing them.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger

from .errors import LiveVideoDisabled, ProviderError
from .types import VideoResult, WanSpec

logger = get_logger("app.providers.video_router")


# --------------------------------------------------------------------------- #
# The backend protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class VideoBackend(Protocol):
    """One source of Wan-class renders the router can route to.

    :class:`~app.providers.video.VideoProvider` satisfies this directly. Future
    backends (a second hosted region, the §12.6 Alibaba worker, a self-hosted lane)
    implement the same three members and drop into a :class:`VideoRouter`.
    """

    #: Stable identity for ordering, health bookkeeping, and telemetry.
    name: str

    async def render(self, spec: WanSpec) -> VideoResult:
        """Render ``spec`` to a clip. Raises ``LiveVideoDisabled`` when gated off."""
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe (no render spend); ``True`` when routable."""
        ...


# --------------------------------------------------------------------------- #
# Per-backend health (pure circuit breaker)
# --------------------------------------------------------------------------- #


class BackendStatus(StrEnum):
    """The circuit state of one backend in the router."""

    CLOSED = "closed"  # healthy; route freely
    OPEN = "open"  # tripped; skip until the cooldown elapses
    HALF_OPEN = "half_open"  # cooldown elapsed; allow one probe attempt


#: A monotonic clock source (seconds). Injectable so tests advance time exactly.
Clock = Callable[[], float]


@dataclass
class BackendHealth:
    """A small per-backend circuit breaker driving router ordering (pure logic).

    Trips ``OPEN`` after ``failure_threshold`` consecutive failures; after
    ``cooldown_s`` it allows a single ``HALF_OPEN`` probe whose outcome either
    closes it (success) or re-opens it (failure). Mirrors the shape of
    :class:`app.providers.base.CircuitBreaker` but is owned by the router so
    ordering decisions never reach into a backend's shared transport.
    """

    name: str
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    _clock: Clock = field(default=time.monotonic, repr=False)
    status: BackendStatus = BackendStatus.CLOSED
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    opened_at: float = 0.0

    def available(self) -> bool:
        """True when the breaker would let a call through right now.

        Transitions ``OPEN`` → ``HALF_OPEN`` in place once the cooldown elapses, so
        callers see the probe window without a separate tick.
        """
        if self.status is BackendStatus.OPEN:
            if self._clock() - self.opened_at >= self.cooldown_s:
                self.status = BackendStatus.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self.total_successes += 1
        self.consecutive_failures = 0
        self.status = BackendStatus.CLOSED

    def record_failure(self) -> None:
        self.total_failures += 1
        self.consecutive_failures += 1
        if self.status is BackendStatus.HALF_OPEN or (
            self.consecutive_failures >= self.failure_threshold
        ):
            self.status = BackendStatus.OPEN
            self.opened_at = self._clock()


# --------------------------------------------------------------------------- #
# Router policy
# --------------------------------------------------------------------------- #


class RouteMode(StrEnum):
    """How the router dispatches a render across its backends."""

    #: Try backends in priority order; advance only on retryable transport faults.
    FAILOVER = "failover"
    #: Start the top-``race_size`` available backends concurrently; first win cancels rest.
    RACE = "race"
    #: Cost-aware failover: when the budget is low, prefer cheaper/turbo backends
    #: first; otherwise prefer quality. Falls back to failover ordering within a tier.
    COST_AWARE = "cost_aware"


@dataclass(frozen=True, slots=True)
class BackendTier:
    """The cost/quality position of one backend, for cost-aware routing.

    Attributes:
        cost_per_s: relative cost of one video-second from this backend (a turbo id
            is cheaper than a quality id; absolute units don't matter, only ratios).
        quality: 0..1 quality score — higher = better fidelity (quality id > turbo).
    """

    cost_per_s: float = 1.0
    quality: float = 0.5


@dataclass(frozen=True, slots=True)
class RouterPolicy:
    """Tunables for :class:`VideoRouter` (deterministic; no env reads)."""

    mode: RouteMode = RouteMode.FAILOVER
    #: How many backends to start concurrently in :data:`RouteMode.RACE`.
    race_size: int = 2
    #: Per-backend breaker trip threshold.
    failure_threshold: int = 3
    #: Per-backend cooldown before a half-open probe.
    cooldown_s: float = 30.0


def order_for_budget(
    backends: Sequence[VideoBackend],
    tiers: Mapping[str, BackendTier],
    *,
    budget_low: bool,
) -> list[VideoBackend]:
    """Order backends by cost when the budget is low, else by quality (pure).

    Budget low → ascending ``cost_per_s`` (cheapest first); budget healthy →
    descending ``quality`` (best first). Backends with no tier sort to a neutral
    middle. Ties keep the input (priority) order — a stable sort over a key — so the
    cost-aware mode degrades to plain failover ordering when tiers are equal/absent.
    """
    indexed = list(enumerate(backends))
    neutral = BackendTier()

    def key(item: tuple[int, VideoBackend]) -> tuple[float, int]:
        idx, backend = item
        tier = tiers.get(backend.name, neutral)
        primary = tier.cost_per_s if budget_low else -tier.quality
        return (primary, idx)

    return [backend for _, backend in sorted(indexed, key=key)]


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #


class VideoRouter:
    """Route a :class:`WanSpec` across ordered :class:`VideoBackend` s.

    Construction takes the backends in **priority order** (first = preferred). The
    router itself implements the :class:`VideoBackend` contract (``name`` /
    ``render`` / ``healthy``), so a router can nest inside another router and the
    Generator cannot tell how many real backends sit underneath.
    """

    def __init__(
        self,
        backends: Sequence[VideoBackend],
        *,
        policy: RouterPolicy | None = None,
        clock: Clock = time.monotonic,
        name: str = "video-router",
        tiers: Mapping[str, BackendTier] | None = None,
    ) -> None:
        if not backends:
            raise ValueError("VideoRouter requires at least one backend")
        self.name = name
        self._policy = policy or RouterPolicy()
        self._backends: list[VideoBackend] = list(backends)
        #: Optional cost/quality position per backend (for COST_AWARE mode). Empty
        #: → every backend is a neutral tier and COST_AWARE == FAILOVER ordering.
        self._tiers: dict[str, BackendTier] = dict(tiers or {})
        self._health: dict[str, BackendHealth] = {
            b.name: BackendHealth(
                name=b.name,
                failure_threshold=self._policy.failure_threshold,
                cooldown_s=self._policy.cooldown_s,
                _clock=clock,
            )
            for b in self._backends
        }

    # -- introspection ---------------------------------------------------- #

    def health(self, name: str) -> BackendHealth:
        """The :class:`BackendHealth` record for backend ``name``."""
        return self._health[name]

    def available_backends(self) -> list[VideoBackend]:
        """Backends whose breaker currently permits a call, in priority order."""
        return [b for b in self._backends if self._health[b.name].available()]

    async def healthy(self) -> bool:
        """True when *any* backend reports healthy (router is routable)."""
        for backend in self.available_backends():
            try:
                if await backend.healthy():
                    return True
            except ProviderError:
                continue
        return False

    # -- render ----------------------------------------------------------- #

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        """Render ``spec`` per the policy. Raises ``LiveVideoDisabled`` when gated.

        ``budget_low`` only matters in :data:`RouteMode.COST_AWARE`: when set, the
        cost-aware order prefers cheaper/turbo backends first (preserve scarce
        video-seconds, §11); otherwise it prefers quality. The plain
        :class:`~app.providers.video_router.VideoBackend` protocol call
        ``render(spec)`` keeps working — ``budget_low`` is an optional router extra.

        Raises:
            LiveVideoDisabled: propagated unchanged from the first backend (the
                gate is closed; nothing was submitted).
            ProviderError: a non-retryable backend error, or — after every backend
                has been tried — the last retryable error encountered.
        """
        if self._policy.mode is RouteMode.RACE:
            return await self._render_racing(spec)
        if self._policy.mode is RouteMode.COST_AWARE:
            return await self._render_failover(spec, budget_low=budget_low)
        return await self._render_failover(spec)

    def _ordered_candidates(self, *, budget_low: bool) -> list[VideoBackend]:
        """The available backends ordered for the active mode (cost-aware or not)."""
        available = self.available_backends()
        if not available:
            return self._backends[:1]
        if self._policy.mode is RouteMode.COST_AWARE and self._tiers:
            return order_for_budget(available, self._tiers, budget_low=budget_low)
        return available

    async def _render_failover(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        candidates = self._ordered_candidates(budget_low=budget_low)
        last_error: ProviderError | None = None
        for backend in candidates:
            try:
                result = await backend.render(spec)
            except LiveVideoDisabled:
                # Deliberate spend gate — not a fault. Propagate unchanged; do NOT
                # mark the backend unhealthy and do NOT try another backend.
                raise
            except ProviderError as exc:
                health = self._health[backend.name]
                health.record_failure()
                last_error = exc
                logger.warning(
                    "video_router.backend_failed",
                    backend=backend.name,
                    retryable=exc.retryable,
                    error=type(exc).__name__,
                    status=exc.status_code,
                )
                if not exc.retryable:
                    # A bad request fails identically everywhere — surface it now.
                    raise
                continue
            self._health[backend.name].record_success()
            logger.info("video_router.routed", backend=backend.name, mode="failover")
            return result
        assert last_error is not None  # at least one candidate always ran
        raise last_error

    async def _render_racing(self, spec: WanSpec) -> VideoResult:
        candidates = self.available_backends()[: max(1, self._policy.race_size)]
        if not candidates:
            candidates = self._backends[:1]
        if len(candidates) == 1:
            # Nothing to race against — degenerate to the failover path's bookkeeping.
            return await self._render_failover(spec)

        tasks: dict[asyncio.Task[VideoResult], VideoBackend] = {}
        for backend in candidates:
            task = asyncio.ensure_future(backend.render(spec))
            tasks[task] = backend

        pending = set(tasks)
        last_error: ProviderError | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    backend = tasks[task]
                    try:
                        result = task.result()
                    except LiveVideoDisabled:
                        # Gate closed on every racer; cancel the rest and propagate.
                        await self._cancel_all(pending)
                        raise
                    except ProviderError as exc:
                        self._health[backend.name].record_failure()
                        last_error = exc
                        logger.warning(
                            "video_router.race_loser",
                            backend=backend.name,
                            error=type(exc).__name__,
                        )
                        continue
                    self._health[backend.name].record_success()
                    await self._cancel_all(pending)
                    logger.info("video_router.routed", backend=backend.name, mode="race")
                    return result
        finally:
            await self._cancel_all(pending)
        assert last_error is not None
        raise last_error

    @staticmethod
    async def _cancel_all(tasks: set[asyncio.Task[VideoResult]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, ProviderError):
                await task


__all__ = [
    "BackendHealth",
    "BackendStatus",
    "BackendTier",
    "Clock",
    "RouteMode",
    "RouterPolicy",
    "VideoBackend",
    "VideoRouter",
    "order_for_budget",
]
