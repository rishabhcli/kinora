"""The throttle **client** — the one object application code holds.

Everything above (algorithms, hierarchy, leases, quota) is machinery; the client
is the ergonomic surface that ties it together and adds the two behaviours every
caller needs:

* **``try_acquire`` vs ``acquire``.** ``try_acquire`` is the non-blocking probe —
  one atomic round-trip, returns a :class:`Verdict` you inspect (including a
  precise ``retry_after`` / ``Retry-After`` for an HTTP 429). ``acquire`` is the
  cooperative blocking form — it waits exactly ``retry_after`` (via an injected
  sleep, so tests advance a fake clock) and retries, up to an optional deadline,
  raising :class:`~app.throttle.errors.Throttled` only if the deadline is hit.

* **Fail-open / fail-closed.** If the backing store is down
  (:class:`~app.throttle.errors.StoreUnavailable`), a *rate limiter is not worth
  an outage*: under the default ``fail_open=True`` the client logs and **admits**
  the request (availability over perfect limiting — the upstream provider's own
  limiter is the backstop). Set ``fail_open=False`` for limits that protect
  something that must never be exceeded (e.g. a hard spend cap), where denying is
  safer than admitting. Either way the choice is explicit and logged.

The client composes a :class:`~app.throttle.hierarchy.HierarchicalLimiter` (the
rate side) with an optional :class:`~app.throttle.leases.ConcurrencyLeasePool`
(the concurrency side). A full ``acquire`` therefore enforces *both* "not too
fast" and "not too many at once", which is exactly the pair a shared external
provider imposes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.logging import get_logger
from app.throttle.clock import Clock, MonotonicClock
from app.throttle.errors import LeaseUnavailable, StoreUnavailable, Throttled
from app.throttle.hierarchy import HierarchicalLimiter, HierarchyDecision
from app.throttle.leases import ConcurrencyLeasePool, Lease

log = get_logger(__name__)

#: Async sleep seam: ``await sleep(seconds)``. Tests wire one that advances a clock.
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Verdict:
    """The result of a non-blocking :meth:`ThrottleClient.try_acquire`.

    ``allowed`` is the verdict; ``retry_after`` is the precise wait when denied
    (suitable verbatim for an HTTP ``Retry-After`` header, rounded up by the
    caller); ``scope`` names the bottleneck; ``remaining`` is the tightest
    headroom across the hierarchy for ``X-RateLimit-Remaining``; ``fail_open``
    is True when this *allow* was granted because the store was down (so callers
    can surface degraded-mode telemetry).
    """

    allowed: bool
    retry_after: float = 0.0
    scope: str = ""
    remaining: float = 0.0
    fail_open: bool = False

    def retry_after_seconds_ceil(self) -> int:
        """``Retry-After`` is an integer seconds header; round up so we never
        advertise a wait shorter than the real one."""
        import math

        return max(0, math.ceil(self.retry_after))


class ThrottleClient:
    """Holds a rate hierarchy + optional concurrency pool; the caller's entrypoint.

    :param hierarchy: the rate-limit hierarchy (global→provider→tenant→endpoint).
    :param lease_pool: optional fleet-wide concurrency cap enforced alongside.
    :param fail_open: on store failure, admit (True, default) or deny (False).
    :param clock: time seam for blocking-acquire deadlines (the limiters use the
        transport's server clock; this clock only bounds the *client's* waiting).
    :param sleep: async sleep seam for blocking acquire.
    """

    def __init__(
        self,
        hierarchy: HierarchicalLimiter,
        *,
        lease_pool: ConcurrencyLeasePool | None = None,
        fail_open: bool = True,
        clock: Clock | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self._hierarchy = hierarchy
        self._lease_pool = lease_pool
        self._fail_open = fail_open
        self._clock = clock or MonotonicClock()
        if sleep is None:
            import asyncio

            sleep = asyncio.sleep
        self._sleep = sleep

    @property
    def fail_open(self) -> bool:
        return self._fail_open

    async def try_acquire(self, cost: float = 1.0) -> Verdict:
        """One non-blocking round-trip. Never raises for a *policy* denial — it is
        reported in the :class:`Verdict`. A store failure is handled by the
        fail-open/closed policy (admit or a denying verdict)."""
        try:
            decision = await self._hierarchy.acquire(cost)
        except StoreUnavailable as exc:
            return self._on_store_failure(exc)
        return self._verdict_from(decision)

    async def acquire(
        self,
        cost: float = 1.0,
        *,
        max_wait: float | None = None,
    ) -> Verdict:
        """Cooperatively block until admitted or ``max_wait`` (wall seconds) elapses.

        Waits exactly the limiter's ``retry_after`` between attempts — no busy
        spin, no fixed poll — so the wait is as short as correctness allows.
        Raises :class:`Throttled` if ``max_wait`` is exhausted; on store failure,
        defers to the fail-open/closed policy like :meth:`try_acquire`.
        """
        deadline = None if max_wait is None else self._clock.now() + max_wait
        while True:
            verdict = await self.try_acquire(cost)
            if verdict.allowed:
                return verdict
            wait = verdict.retry_after
            now = self._clock.now()
            if deadline is not None and now + wait > deadline:
                raise Throttled(
                    verdict.retry_after,
                    scope=verdict.scope,
                    limit="hierarchy",
                )
            # A zero wait (a race that just cleared) still yields control once.
            await self._sleep(max(wait, 0.0))

    @asynccontextmanager
    async def guard(
        self,
        cost: float = 1.0,
        *,
        max_wait: float | None = None,
    ) -> AsyncIterator[Lease | None]:
        """Acquire the rate budget *and* (if configured) a concurrency lease for the
        duration of the ``async with`` body, releasing the lease on exit.

        This is the recommended call site for "run this provider request": it
        enforces both not-too-fast (rate) and not-too-many-at-once (lease), and
        guarantees the lease is returned even if the body raises. If a lease pool
        is configured and full, raises :class:`LeaseUnavailable`. Yields the held
        :class:`Lease` (or ``None`` if no pool) so the body can heartbeat it.
        """
        await self.acquire(cost, max_wait=max_wait)
        if self._lease_pool is None:
            yield None
            return
        lease = self._lease_pool.lease()
        acquired, count, retry_after = await self._lease_pool._raw_acquire(lease.holder_id)
        if not acquired:
            # We already spent rate budget; refunding the whole hierarchy here is
            # not worth it (the budget is per-time and will refill), but we must
            # not silently swallow — surface the concurrency denial.
            raise LeaseUnavailable(
                retry_after,
                scope=self._lease_pool.scope,
                in_flight=count,
                capacity=self._lease_pool.capacity,
            )
        lease._held = True
        try:
            yield lease
        finally:
            await lease.release()

    # -- internals -------------------------------------------------------- #

    def _verdict_from(self, decision: HierarchyDecision) -> Verdict:
        if decision.allowed:
            return Verdict(
                allowed=True,
                remaining=decision.min_remaining,
            )
        return Verdict(
            allowed=False,
            retry_after=decision.retry_after,
            scope=decision.binding,
            remaining=decision.min_remaining,
        )

    def _on_store_failure(self, exc: StoreUnavailable) -> Verdict:
        if self._fail_open:
            log.warning(
                "throttle.store_unavailable.fail_open",
                error=str(exc),
                action="admit",
            )
            return Verdict(allowed=True, fail_open=True)
        log.warning(
            "throttle.store_unavailable.fail_closed",
            error=str(exc),
            action="deny",
        )
        # Fail-closed: deny with a modest retry hint so the caller backs off and
        # the store can recover, rather than hammering it.
        return Verdict(allowed=False, retry_after=1.0, scope="store")


__all__ = [
    "SleepFn",
    "ThrottleClient",
    "Verdict",
]
