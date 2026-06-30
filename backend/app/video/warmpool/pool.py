"""Per-provider warm session pool — the heart of the warm-pool subsystem.

One :class:`ProviderPool` owns the warm (idle, ready) sessions and the leased
sessions for a single video provider. It maintains a ``min_warm`` floor (raised
toward a demand-driven ``warm_target`` by the keep-alive scheduler), evicts idle
sessions past their TTL, recycles stale/unhealthy ones, lends sessions under a
fair FIFO queue with a borrow timeout, and *drains* (closes everything, refuses
new lends) when the provider's circuit is open.

Purity / determinism: every I/O touch (open, probe, close) goes through the
injected :class:`~app.video.warmpool.protocols.SessionFactory` /
:class:`~app.video.warmpool.protocols.ProviderSession`; every deadline goes through
the injected :class:`~app.video.warmpool.clock.Clock`. A single ``asyncio.Lock``
serialises mutations of the warm/leased sets so the invariants hold under
concurrency without sprinkling races. **No warm-session leak** is the load-bearing
invariant: every session is either warm, leased, or closed — never lost.

It never renders and never reads ``KINORA_LIVE_VIDEO``; it manages connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import deque
from dataclasses import dataclass

from app.core.logging import get_logger

from .clock import Clock
from .cost import ColdStartModel
from .lease import FairWaiterQueue, Lease, LeaseTimeout, PoolDraining
from .protocols import HealthSignal, ProviderId, ProviderSession, SessionFactory
from .settings import WarmPoolConfig

logger = get_logger("app.video.warmpool.pool")


@dataclass(slots=True)
class _Entry:
    """A warm session plus the timestamps the pool ages it against."""

    session: ProviderSession
    created_at: float
    idle_since: float
    last_check_at: float


@dataclass(slots=True)
class PoolStats:
    """A point-in-time snapshot of a provider pool (telemetry / assertions)."""

    provider: ProviderId
    warm: int
    leased: int
    total: int
    waiting: int
    draining: bool
    warm_target: int
    opens: int
    closes: int
    borrows: int
    cold_borrows: int
    timeouts: int
    health_recycles: int
    idle_evictions: int


class ProviderPool:
    """The warm session pool for one provider (async, bounded, fair, drainable)."""

    def __init__(
        self,
        provider: ProviderId,
        factory: SessionFactory,
        *,
        clock: Clock,
        config: WarmPoolConfig,
        cost: ColdStartModel | None = None,
        health: HealthSignal | None = None,
    ) -> None:
        self.provider = provider
        self._factory = factory
        self._clock = clock
        self._config = config
        self._health = health
        self.cost = cost or ColdStartModel(provider=provider)

        self._lock = asyncio.Lock()
        self._warm: deque[_Entry] = deque()
        self._leased: dict[int, ProviderSession] = {}
        self._waiters = FairWaiterQueue(_clock=clock)
        self._lease_ids = itertools.count(1)

        #: Demand-driven warm floor the keep-alive scheduler writes; clamped into
        #: ``[min_warm-or-0, max_warm]`` by the demand model before it lands here.
        self.warm_target = config.min_warm
        self._draining = False
        self._closed = False

        # counters (telemetry; cheap)
        self._opens = 0
        self._closes = 0
        self._borrows = 0
        self._cold_borrows = 0
        self._timeouts = 0
        self._health_recycles = 0
        self._idle_evictions = 0

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def total(self) -> int:
        """Warm + leased — what counts against ``max_size``."""
        return len(self._warm) + len(self._leased)

    def stats(self) -> PoolStats:
        return PoolStats(
            provider=self.provider,
            warm=len(self._warm),
            leased=len(self._leased),
            total=self.total,
            waiting=self._waiters.waiting,
            draining=self._draining,
            warm_target=self.warm_target,
            opens=self._opens,
            closes=self._closes,
            borrows=self._borrows,
            cold_borrows=self._cold_borrows,
            timeouts=self._timeouts,
            health_recycles=self._health_recycles,
            idle_evictions=self._idle_evictions,
        )

    # ------------------------------------------------------------------ #
    # session lifecycle (I/O goes through the factory; timed for the cost model)
    # ------------------------------------------------------------------ #

    async def _open_session(self) -> ProviderSession:
        """Open a fresh session, timing the cold-start latency into the cost model."""
        start = self._clock.monotonic()
        session = await self._factory.open(self.provider)
        elapsed = max(0.0, self._clock.monotonic() - start)
        self._opens += 1
        self.cost.record_cold_open(elapsed)
        return session

    async def _close_session(self, session: ProviderSession) -> None:
        """Close a session, swallowing close errors (best-effort reclaim)."""
        self._closes += 1
        with contextlib.suppress(Exception):
            await session.close()

    async def _is_usable(self, entry: _Entry, *, now: float) -> bool:
        """True if ``entry`` can be lent: not too old, and (if due) probes healthy.

        Re-probes only when the session has been idle past ``health_check_interval_s``
        (cheap-but-not-free), and recycles anything past ``max_session_age_s``.
        """
        cfg = self._config
        if now - entry.created_at >= cfg.max_session_age_s:
            return False
        if now - entry.last_check_at >= cfg.health_check_interval_s:
            entry.last_check_at = now
            try:
                ok = await entry.session.healthy()
            except Exception:
                ok = False
            if not ok:
                return False
        return True

    # ------------------------------------------------------------------ #
    # borrow / return — the lease API
    # ------------------------------------------------------------------ #

    def borrow(self, *, timeout_s: float | None = None) -> _BorrowCtx:
        """Borrow a session as an async context manager.

        ``async with pool.borrow() as lease: lease.handle.render(spec)``. The
        session is returned automatically on exit (success or error). Raises
        :class:`PoolDraining` if the provider is unhealthy and
        :class:`LeaseTimeout` if no session frees within the deadline.
        """
        effective = timeout_s if timeout_s is not None else self._config.borrow_timeout_s
        return _BorrowCtx(self, effective)

    async def _acquire(self, timeout_s: float) -> Lease:
        deadline = self._clock.monotonic() + max(0.0, timeout_s)
        while True:
            async with self._lock:
                if self._closed or self._draining:
                    raise PoolDraining(self.provider)
                # 1) hand out a usable warm session if any.
                now = self._clock.monotonic()
                session = await self._take_warm_locked(now=now)
                if session is not None:
                    return self._lease_locked(session, cold=False, now=now)
                # 2) room to grow → open a cold session and lend it.
                if self.total < self._config.max_size:
                    # Reserve the slot before releasing the lock by leasing a
                    # placeholder is overkill; instead open under the lock-free
                    # section but count it via a pending reservation.
                    return await self._open_and_lease_locked(now=now)
                # 3) exhausted → park as a fair waiter.
                fut = self._waiters.park()
            # ---- released lock: wait for a hand-off or timeout ----
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                fut.cancel()
                async with self._lock:
                    self._waiters.drop_cancelled()
                    self._timeouts += 1
                raise LeaseTimeout(self.provider, max(0.0, timeout_s))
            handed = await self._wait_for_handoff(fut, remaining)
            if isinstance(handed, _TimeoutMarker):  # deadline passed without a hand-off
                async with self._lock:
                    self._waiters.drop_cancelled()
                    self._timeouts += 1
                raise LeaseTimeout(self.provider, max(0.0, timeout_s))
            # got a handed-off session → register the lease.
            async with self._lock:
                if self._closed or self._draining:
                    await self._close_session(handed)
                    raise PoolDraining(self.provider)
                now = self._clock.monotonic()
                return self._lease_locked(handed, cold=False, now=now)

    async def _wait_for_handoff(
        self, fut: asyncio.Future[ProviderSession], remaining: float
    ) -> ProviderSession | _TimeoutMarker:
        """Await the single waiter future; a timer resolves it with ``_TIMED_OUT``.

        Only one future is awaited (no ``asyncio.wait`` two-hop), so a single
        ``VirtualClock.advance`` past the deadline deterministically wakes the
        waiter: the timer coroutine fires, sets ``_TIMED_OUT`` on ``fut`` (unless a
        hand-off already resolved it), and the lone ``await fut`` resumes. The
        ``FairWaiterQueue.handoff`` path checks ``fut.done()`` so a hand-off racing
        a just-fired timeout is harmless either way.
        """

        async def _timer() -> None:
            await self._clock.sleep(remaining)
            if not fut.done():
                fut.set_result(_TIMED_OUT)  # type: ignore[arg-type]

        timer = asyncio.ensure_future(_timer())
        try:
            return await fut
        finally:
            if not timer.done():
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer

    async def _take_warm_locked(self, *, now: float) -> ProviderSession | None:
        """Pop the next usable warm session (recycling unusable ones). Lock held."""
        while self._warm:
            entry = self._warm.popleft()
            if await self._is_usable(entry, now=now):
                self.cost.record_warm_borrow(0.0)
                return entry.session
            # unusable → recycle and keep looking.
            self._health_recycles += 1
            await self._close_session(entry.session)
        return None

    async def _open_and_lease_locked(self, *, now: float) -> Lease:
        """Open a cold session while holding the slot, then lease it. Lock held.

        We register a placeholder lease id *before* awaiting the open so the slot
        is reserved against ``max_size``; if the open fails we release it.
        """
        lease_id = next(self._lease_ids)
        self._leased[lease_id] = _OPENING  # reserve the slot
        try:
            session = await self._open_session()
        except Exception:
            del self._leased[lease_id]
            raise
        self._leased[lease_id] = session
        self._borrows += 1
        self._cold_borrows += 1
        return Lease(
            provider=self.provider,
            session=session,
            leased_at=now,
            lease_id=lease_id,
        )

    def _lease_locked(self, session: ProviderSession, *, cold: bool, now: float) -> Lease:
        """Register a warm session as leased and build its lease handle. Lock held."""
        lease_id = next(self._lease_ids)
        self._leased[lease_id] = session
        self._borrows += 1
        if cold:
            self._cold_borrows += 1
        return Lease(provider=self.provider, session=session, leased_at=now, lease_id=lease_id)

    async def _return(self, lease: Lease) -> None:
        """Return a leased session: hand off to a waiter, re-shelve, or close."""
        if lease.returned:
            return
        lease.returned = True
        async with self._lock:
            self._leased.pop(lease.lease_id, None)
            now = self._clock.monotonic()
            # If draining/closed, don't keep the session.
            if self._closed or self._draining:
                await self._close_session(lease.session)
                return
            # Validate freshness before re-circulating.
            entry = _Entry(
                session=lease.session, created_at=lease.leased_at, idle_since=now, last_check_at=now
            )
            if now - entry.created_at >= self._config.max_session_age_s:
                self._health_recycles += 1
                await self._close_session(lease.session)
                await self._maybe_open_for_waiter_locked(now=now)
                return
            # Prefer handing a freed session straight to the oldest waiter (fair).
            if self._waiters.handoff(lease.session):
                # Hand-off counts as a borrow on the waiter's behalf.
                self._borrows += 1
                return
            # Otherwise re-shelve as warm (subject to warm_target on the next sweep).
            self._warm.append(entry)

    async def _maybe_open_for_waiter_locked(self, *, now: float) -> None:
        """If a closed session left a waiter unserved and room exists, open one. Lock held."""
        if self._waiters.waiting == 0:
            return
        if self.total >= self._config.max_size:
            return
        try:
            session = await self._open_session()
        except Exception:
            return
        if not self._waiters.handoff(session):
            # nobody left — shelf it.
            self._warm.append(
                _Entry(session=session, created_at=now, idle_since=now, last_check_at=now)
            )

    # ------------------------------------------------------------------ #
    # keep-alive maintenance — called each scheduler tick
    # ------------------------------------------------------------------ #

    async def maintain(self) -> None:
        """One maintenance pass: drain-if-unhealthy, evict idle, recycle stale, top up.

        Idempotent and safe to call on a cadence. All structural changes happen
        under the lock; opens/closes await the factory but the sets are consistent
        at every yield point.
        """
        if self._closed:
            return
        # 1) circuit-aware drain: if the provider's breaker is open, drain warm.
        if self._health is not None and not self._health.available():
            await self.drain()
            return
        # If we were draining and health recovered, resume.
        self._draining = False

        async with self._lock:
            now = self._clock.monotonic()
            await self._evict_and_recycle_locked(now=now)
            target = max(0, min(self.warm_target, self._config.max_warm))
        # 2) top up to the warm target (outside the lock for the opens, then re-lock).
        await self._top_up_to(target)

    async def _evict_and_recycle_locked(self, *, now: float) -> None:
        """Recycle stale/unhealthy warm sessions and evict idle ones past TTL. Lock held.

        Eviction respects the warm target: we never evict below ``min(target,len)``
        for idleness, but stale/unhealthy sessions are recycled regardless (a dead
        connection below the floor is replaced on top-up, not kept).
        """
        cfg = self._config
        target = max(0, min(self.warm_target, cfg.max_warm))
        kept: deque[_Entry] = deque()
        # Walk oldest-first; recycle unusable, evict surplus-idle.
        while self._warm:
            entry = self._warm.popleft()
            usable = await self._is_usable(entry, now=now)
            if not usable:
                self._health_recycles += 1
                await self._close_session(entry.session)
                continue
            idle_for = now - entry.idle_since
            # Evict only surplus sessions (those above the target) that are stale-idle.
            surplus = (len(kept) + len(self._warm)) >= target
            if surplus and idle_for >= cfg.idle_ttl_s:
                self._idle_evictions += 1
                await self._close_session(entry.session)
                continue
            kept.append(entry)
        self._warm = kept

    async def _top_up_to(self, target: int) -> None:
        """Open sessions until warm count reaches ``target`` (bounded by ``max_size``)."""
        while True:
            async with self._lock:
                if self._closed or self._draining:
                    return
                need = target - len(self._warm)
                if need <= 0 or self.total >= self._config.max_size:
                    return
                # reserve a warm slot by counting against max_size during the open.
            # open outside the lock (it may await the factory / cold-start).
            try:
                session = await self._open_session()
            except Exception:
                logger.warning("warmpool.topup.open_failed", provider=self.provider)
                return
            async with self._lock:
                if self._closed or self._draining or self.total >= self._config.max_size:
                    await self._close_session(session)
                    return
                now = self._clock.monotonic()
                # a waiter may have appeared while we were opening — serve it first.
                if self._waiters.waiting > 0 and self._waiters.handoff(session):
                    self._borrows += 1
                    continue
                self._warm.append(
                    _Entry(session=session, created_at=now, idle_since=now, last_check_at=now)
                )

    # ------------------------------------------------------------------ #
    # drain / close
    # ------------------------------------------------------------------ #

    async def drain(self) -> None:
        """Close every warm session and refuse new lends (leased ones return → close).

        Used when the provider's circuit is open. Leased sessions are *not* yanked
        from their borrowers; they close on return. Parked waiters are failed with
        :class:`PoolDraining` so they don't hang forever.
        """
        async with self._lock:
            self._draining = True
            warm = list(self._warm)
            self._warm.clear()
            self._waiters.fail_all(PoolDraining(self.provider))
        for entry in warm:
            await self._close_session(entry.session)

    async def aclose(self) -> None:
        """Permanently close the pool: drain warm + leased and reject everything."""
        async with self._lock:
            self._closed = True
            self._draining = True
            warm = list(self._warm)
            self._warm.clear()
            leased = list(self._leased.values())
            self._leased.clear()
            self._waiters.fail_all(PoolDraining(self.provider))
        for entry in warm:
            await self._close_session(entry.session)
        for session in leased:
            if session is _OPENING:  # reserved-but-not-opened slot
                continue
            await self._close_session(session)


#: Sentinel for a reserved-but-not-yet-opened lease slot (counts against max_size).
class _OpeningSentinel:
    provider = "<opening>"
    session_id = "<opening>"

    @property
    def handle(self) -> object:  # pragma: no cover - never used
        raise RuntimeError("opening sentinel has no handle")

    async def healthy(self) -> bool:  # pragma: no cover
        return False

    async def close(self) -> None:  # pragma: no cover
        return None


_OPENING: ProviderSession = _OpeningSentinel()


class _TimeoutMarker:
    """Sentinel resolved onto a waiter future when its borrow deadline elapses."""

    __slots__ = ()


#: The single shared timeout sentinel (identity-compared in ``_acquire``).
_TIMED_OUT = _TimeoutMarker()


class _BorrowCtx:
    """Async-context-manager wrapper that acquires on enter and returns on exit."""

    __slots__ = ("_lease", "_pool", "_timeout_s")

    def __init__(self, pool: ProviderPool, timeout_s: float) -> None:
        self._pool = pool
        self._timeout_s = timeout_s
        self._lease: Lease | None = None

    async def __aenter__(self) -> Lease:
        self._lease = await self._pool._acquire(self._timeout_s)
        return self._lease

    async def __aexit__(self, *exc: object) -> None:
        if self._lease is not None:
            await self._pool._return(self._lease)

    def __await__(self):  # type: ignore[no-untyped-def]
        """Allow ``lease = await pool.borrow()`` (caller must return it manually)."""
        return self._pool._acquire(self._timeout_s).__await__()


__all__ = ["PoolStats", "ProviderPool"]
