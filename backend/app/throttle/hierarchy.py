"""The **hierarchical limit model**: many limits enforced together, most-restrictive
wins, with all-or-nothing rollback.

Kinora's load on a provider must respect several limits *simultaneously*:

* a **global** cap (the whole fleet's total DashScope budget),
* a **per-provider** cap (DashScope-intl's published RPS),
* a **per-tenant / per-book** cap (no single book may monopolise the fleet),
* a **per-endpoint** cap (image-gen is throttled separately from video — the
  ``429 Throttling.RateQuota`` in CLAUDE.md is on the *image* model only).

A request must satisfy **all** of them. The semantics are: try to consume from
each limit; if *any* denies, the request is denied with the **largest** of the
binding ``retry_after`` values (wait that long and every limit will admit) — this
is "most-restrictive wins". Crucially, the limits that *did* consume before the
denial are **refunded**, so a deny is side-effect-free across the hierarchy: one
exhausted endpoint limit can't slowly drain the global bucket on every rejected
attempt.

Ordering matters for efficiency: we check **broadest first** (global, then
provider, then tenant, then endpoint) so a global outage rejects with the least
work and the fewest speculative debits to roll back. The rollback makes the
whole ``acquire`` atomic from the caller's view even though redis has no
cross-key transaction here — we compensate rather than lock, the saga pattern.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.throttle.result import Decision

#: An admitted limit's compensator: ``await refund()`` returns the consumed units.
Refunder = Callable[[], Awaitable[None]]


class Limit(Protocol):
    """One enforceable limit: try to consume ``cost`` and, if admitted, hand back a
    compensator that exactly undoes this consumption.

    All three algorithms adapt to this via :class:`LimitNode`. Returning the
    compensator (rather than a bare bool) is what makes hierarchical rollback
    possible — the hierarchy collects the compensators of the limits that admitted
    and runs them if a later limit denies.
    """

    @property
    def scope(self) -> str: ...

    async def acquire(self, cost: float) -> tuple[Decision, Refunder]: ...


@dataclass(frozen=True, slots=True)
class _NoopRefunder:
    """A compensator that does nothing — for a denied limit (nothing consumed)."""

    async def __call__(self) -> None:
        return None


class TokenBucketLimit:
    """Adapt a :class:`~app.throttle.algorithms.TokenBucketLimiter` to :class:`Limit`."""

    def __init__(self, limiter: object) -> None:
        self._limiter = limiter  # TokenBucketLimiter (duck-typed to avoid cycle)

    @property
    def scope(self) -> str:
        return self._limiter.scope  # type: ignore[attr-defined]

    async def acquire(self, cost: float) -> tuple[Decision, Refunder]:
        decision: Decision = await self._limiter.check(cost)  # type: ignore[attr-defined]
        if not decision.allowed:
            return decision, _NoopRefunder()

        async def _refund() -> None:
            await self._limiter.refund(cost)  # type: ignore[attr-defined]

        return decision, _refund


class GcraLimit:
    """Adapt a :class:`~app.throttle.algorithms.GcraLimiter` to :class:`Limit`."""

    def __init__(self, limiter: object) -> None:
        self._limiter = limiter

    @property
    def scope(self) -> str:
        return self._limiter.scope  # type: ignore[attr-defined]

    async def acquire(self, cost: float) -> tuple[Decision, Refunder]:
        decision: Decision = await self._limiter.check(cost)  # type: ignore[attr-defined]
        if not decision.allowed:
            return decision, _NoopRefunder()

        async def _refund() -> None:
            await self._limiter.refund(cost)  # type: ignore[attr-defined]

        return decision, _refund


class SlidingWindowLimit:
    """Adapt a :class:`~app.throttle.algorithms.SlidingWindowLimiter` to :class:`Limit`.

    The sliding-window refund needs the *seed* of the admission, so this adapter
    captures it in the compensator closure — the reason :class:`Limit` returns a
    compensator rather than a generic ``refund(cost)``.
    """

    def __init__(self, limiter: object) -> None:
        self._limiter = limiter

    @property
    def scope(self) -> str:
        return self._limiter.scope  # type: ignore[attr-defined]

    async def acquire(self, cost: float) -> tuple[Decision, Refunder]:
        icost = int(cost)
        if icost != cost:
            raise ValueError("sliding-window cost must be a whole number of slots")
        decision, seed = await self._limiter._check_with_seed(icost)  # type: ignore[attr-defined]
        if not decision.allowed:
            return decision, _NoopRefunder()

        async def _refund() -> None:
            await self._limiter.refund(icost, seed)  # type: ignore[attr-defined]

        return decision, _refund


@dataclass(frozen=True, slots=True)
class HierarchyDecision:
    """The outcome of a whole-hierarchy acquire.

    ``allowed`` is the conjunction of every level. When denied, ``retry_after`` is
    the max across the binding levels and ``binding`` names the level that forced
    the largest wait (the bottleneck) — the single most useful diagnostic. When
    allowed, ``levels`` carries each level's :class:`Decision` for header math
    (e.g. the *minimum* remaining across levels is the honest ``X-RateLimit-Remaining``).
    """

    allowed: bool
    retry_after: float
    binding: str
    levels: tuple[Decision, ...]

    @property
    def min_remaining(self) -> float:
        """The tightest remaining headroom across levels (for response headers)."""
        if not self.levels:
            return 0.0
        return min(d.remaining for d in self.levels)


class HierarchicalLimiter:
    """Enforce an ordered list of :class:`Limit`\\ s together (most-restrictive wins).

    Order the limits **broadest → narrowest** (global, provider, tenant, endpoint)
    so a denial at a broad level short-circuits before narrow levels are touched,
    minimising both work and rollback. On any denial every already-admitted level
    is refunded, so the call is side-effect-free unless it fully succeeds.
    """

    def __init__(self, limits: list[Limit]) -> None:
        if not limits:
            raise ValueError("a hierarchy needs at least one limit")
        self._limits = limits

    async def acquire(self, cost: float = 1.0) -> HierarchyDecision:
        """Try to consume ``cost`` from every level; all-or-nothing.

        Short-circuits on the first denial (the broad-first ordering makes this the
        cheapest common case), refunds whatever already consumed, and reports the
        binding level. On full success returns ``allowed=True`` with every level's
        decision.
        """
        admitted: list[tuple[Decision, Refunder]] = []
        decisions: list[Decision] = []
        for limit in self._limits:
            decision, refunder = await limit.acquire(cost)
            decisions.append(decision)
            if not decision.allowed:
                # Roll back everything admitted before this denial.
                await self._rollback(admitted)
                return HierarchyDecision(
                    allowed=False,
                    retry_after=decision.retry_after,
                    binding=decision.scope or limit.scope,
                    levels=tuple(decisions),
                )
            admitted.append((decision, refunder))
        return HierarchyDecision(
            allowed=True,
            retry_after=0.0,
            binding="",
            levels=tuple(decisions),
        )

    async def acquire_strict_max(self, cost: float = 1.0) -> HierarchyDecision:
        """Like :meth:`acquire` but probes **all** levels even past the first deny.

        Used when the caller wants the *true* maximum wait across every level (so a
        single back-off clears all of them at once), at the cost of touching every
        level. Still all-or-nothing: every admitted level is refunded on denial.
        """
        admitted: list[tuple[Decision, Refunder]] = []
        decisions: list[Decision] = []
        any_denied = False
        for limit in self._limits:
            decision, refunder = await limit.acquire(cost)
            decisions.append(decision)
            if decision.allowed:
                admitted.append((decision, refunder))
            else:
                any_denied = True
        if not any_denied:
            return HierarchyDecision(True, 0.0, "", tuple(decisions))
        # Refund everything that admitted; report the worst wait.
        await self._rollback(admitted)
        worst = max(decisions, key=lambda d: (not d.allowed, d.retry_after))
        return HierarchyDecision(
            allowed=False,
            retry_after=max((d.retry_after for d in decisions if not d.allowed), default=0.0),
            binding=worst.scope,
            levels=tuple(decisions),
        )

    @staticmethod
    async def _rollback(admitted: list[tuple[Decision, Refunder]]) -> None:
        # Compensate in reverse admission order (LIFO), the saga convention; here
        # the refunds are independent so order is cosmetic, but LIFO keeps it
        # consistent with how a transaction unwinds.
        for _decision, refunder in reversed(admitted):
            await refunder()


__all__ = [
    "GcraLimit",
    "HierarchicalLimiter",
    "HierarchyDecision",
    "Limit",
    "Refunder",
    "SlidingWindowLimit",
    "TokenBucketLimit",
]
