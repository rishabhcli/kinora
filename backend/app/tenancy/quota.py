"""Per-tenant quota + spend envelopes, composing with the global budget.

Kinora's load-bearing guardrail is the *global* video-seconds ceiling
(:class:`app.memory.budget_service.BudgetService`, kinora.md §11.1). A platform
needs a second envelope *per tenant*: an org buys a plan that caps its book
count, its monthly USD spend, and its monthly video-seconds — and that cap must
**compose** with the global ceiling so the binding limit is always the *smaller*
of (what's left in the tenant's envelope, what's left globally).

This module is pure arithmetic over value objects:

* :class:`QuotaEnvelope` — the caps for one tenant (book count, monthly USD,
  monthly video-seconds). ``0`` on a cap means *unlimited-by-this-envelope*.
* :class:`Usage` — what the tenant has already consumed this period.
* :class:`GlobalState` — how much head-room the global ceiling still has.
* :func:`evaluate` — the composing decision: given a requested charge, return a
  :class:`QuotaDecision` (allowed?, the binding scope, the remaining head-room).
* :func:`reserve` — raise :class:`QuotaExceeded` if a charge would breach either
  envelope; the inverse of the budget service's ``reserve`` at the tenant tier.

It deliberately holds no I/O — a repository protocol
(:class:`app.tenancy.domain.QuotaRepo`) supplies the persisted usage, and the
in-memory fake makes the enforcement exhaustively testable with no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class QuotaResource(StrEnum):
    """The metered resources a tenant envelope bounds."""

    BOOKS = "books"
    USD = "usd"
    VIDEO_SECONDS = "video_seconds"


class QuotaScope(StrEnum):
    """Which envelope bound a decision (for the binding-scope report)."""

    NONE = "none"
    TENANT = "tenant"
    GLOBAL = "global"


class QuotaExceeded(RuntimeError):  # noqa: N818 - public contract name
    """Raised when a charge would breach a tenant or global cap."""

    def __init__(
        self,
        resource: QuotaResource,
        scope: QuotaScope,
        *,
        requested: float,
        used: float,
        cap: float,
    ) -> None:
        self.resource = resource
        self.scope = scope
        self.requested = requested
        self.used = used
        self.cap = cap
        super().__init__(
            f"{scope} {resource} quota exceeded: requested {requested:g} "
            f"+ used {used:g} > cap {cap:g}"
        )


@dataclass(frozen=True, slots=True)
class QuotaEnvelope:
    """The per-tenant caps. ``0`` on any cap == unlimited *by this envelope*.

    Caps are still composed with the global ceiling, so ``0`` never means
    "ignore the global limit" — it means "this tenant adds no tighter bound".
    """

    max_books: int = 0
    monthly_usd: float = 0.0
    monthly_video_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_books < 0 or self.monthly_usd < 0 or self.monthly_video_seconds < 0:
            raise ValueError("quota caps must be non-negative (0 == unlimited)")

    def cap_for(self, resource: QuotaResource) -> float:
        """The cap value for a resource (``0`` == unlimited-by-envelope)."""
        if resource is QuotaResource.BOOKS:
            return float(self.max_books)
        if resource is QuotaResource.USD:
            return self.monthly_usd
        return self.monthly_video_seconds


@dataclass(frozen=True, slots=True)
class Usage:
    """What a tenant has consumed in the current billing period."""

    books: int = 0
    usd: float = 0.0
    video_seconds: float = 0.0

    def used_for(self, resource: QuotaResource) -> float:
        if resource is QuotaResource.BOOKS:
            return float(self.books)
        if resource is QuotaResource.USD:
            return self.usd
        return self.video_seconds

    def with_charge(self, resource: QuotaResource, amount: float) -> Usage:
        """A copy with ``amount`` added to ``resource`` (post-commit usage)."""
        if resource is QuotaResource.BOOKS:
            return replace(self, books=self.books + int(amount))
        if resource is QuotaResource.USD:
            return replace(self, usd=self.usd + amount)
        return replace(self, video_seconds=self.video_seconds + amount)


@dataclass(frozen=True, slots=True)
class GlobalState:
    """Head-room left in the global video-seconds ceiling (§11.1 composition).

    ``video_seconds_remaining`` of a negative or ``inf`` value disables the
    global bound (the latter is the natural default when composition isn't
    wired). Only video-seconds compose globally today; USD/books are
    tenant-local, matching the budget service which meters only video-seconds.
    """

    video_seconds_remaining: float = float("inf")


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """The outcome of composing a requested charge with both envelopes."""

    resource: QuotaResource
    requested: float
    allowed: bool
    binding_scope: QuotaScope
    #: Smallest remaining head-room across the composed envelopes.
    remaining: float
    #: ``cap - used`` within the tenant envelope (``inf`` if unlimited).
    tenant_remaining: float
    #: Global head-room (``inf`` for non-video resources / unbounded global).
    global_remaining: float

    @property
    def denied(self) -> bool:
        return not self.allowed


def _tenant_remaining(envelope: QuotaEnvelope, usage: Usage, resource: QuotaResource) -> float:
    cap = envelope.cap_for(resource)
    if cap == 0:  # unlimited-by-envelope
        return float("inf")
    return cap - usage.used_for(resource)


def _global_remaining(global_state: GlobalState, resource: QuotaResource) -> float:
    if resource is QuotaResource.VIDEO_SECONDS:
        rem = global_state.video_seconds_remaining
        return rem if rem >= 0 else 0.0
    return float("inf")


def evaluate(
    resource: QuotaResource,
    requested: float,
    *,
    envelope: QuotaEnvelope,
    usage: Usage,
    global_state: GlobalState | None = None,
) -> QuotaDecision:
    """Compose the tenant envelope with the global ceiling for one charge.

    The binding scope is whichever envelope has the *smaller* remaining
    head-room; the charge is allowed iff it fits within that smaller bound.
    """
    if requested < 0:
        raise ValueError("requested charge must be non-negative")
    gs = global_state if global_state is not None else GlobalState()

    tenant_rem = _tenant_remaining(envelope, usage, resource)
    global_rem = _global_remaining(gs, resource)

    remaining = min(tenant_rem, global_rem)
    # The binding scope is whichever envelope has the smaller (finite) head-room.
    # When both are unlimited there is no binding scope.
    if global_rem <= tenant_rem and global_rem != float("inf"):
        binding = QuotaScope.GLOBAL
    elif tenant_rem != float("inf"):
        binding = QuotaScope.TENANT
    else:
        binding = QuotaScope.NONE

    return QuotaDecision(
        resource=resource,
        requested=requested,
        allowed=requested <= remaining,
        binding_scope=binding,
        remaining=remaining,
        tenant_remaining=tenant_rem,
        global_remaining=global_rem,
    )


def reserve(
    resource: QuotaResource,
    requested: float,
    *,
    envelope: QuotaEnvelope,
    usage: Usage,
    global_state: GlobalState | None = None,
) -> QuotaDecision:
    """Like :func:`evaluate` but raise :class:`QuotaExceeded` when denied."""
    decision = evaluate(
        resource,
        requested,
        envelope=envelope,
        usage=usage,
        global_state=global_state,
    )
    if decision.denied:
        scope = decision.binding_scope
        if scope is QuotaScope.GLOBAL:
            used = 0.0
            cap = decision.global_remaining
        else:
            scope = QuotaScope.TENANT
            used = usage.used_for(resource)
            cap = envelope.cap_for(resource)
        raise QuotaExceeded(resource, scope, requested=requested, used=used, cap=cap)
    return decision


__all__ = [
    "GlobalState",
    "QuotaDecision",
    "QuotaEnvelope",
    "QuotaExceeded",
    "QuotaResource",
    "QuotaScope",
    "Usage",
    "evaluate",
    "reserve",
]
