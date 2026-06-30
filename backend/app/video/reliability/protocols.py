"""Local collaborator Protocols for the reliability coordinator.

FINAL-ROUND constraint: rounds 1 & 2 are **not** merged, so this subsystem cannot
import the real router / governor / cost / quality / jobs packages. Instead it
depends only on these minimal, structural :class:`typing.Protocol` seams — the
*shape* of what it needs from each collaborator. The orchestrator binds the real
implementations later (a real ``ProviderRouter`` over the resilience gateway, the
real budget/quota governor, etc.); tests bind scripted fakes. Nothing here imports
infra or another round's code.

Each Protocol is intentionally tiny — only the methods the coordinator actually
calls — so adapting a real component is a thin shim, not a rewrite.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.video.reliability.models import RenderResult, ShotSpec


@runtime_checkable
class GovernorProtocol(Protocol):
    """Capacity / SLA admission control (round-2 ``governor``).

    Answers, per provider, "may we send work there right now?" — folding in
    quota, rolling SLA health, and load-shedding under pressure. The coordinator
    uses :meth:`admit` to prune the candidate set and :meth:`load_factor` (lower
    = more headroom) as a ranking signal.
    """

    def admit(self, provider: str, shot: ShotSpec) -> bool:
        """Return True if the provider may currently accept this shot."""
        ...

    def load_factor(self, provider: str) -> float:
        """Current load/SLA pressure in [0,1] (0 = idle/healthy, 1 = saturated)."""
        ...


@runtime_checkable
class QualityReputationProtocol(Protocol):
    """Historical quality reputation per provider (round-2 ``quality``).

    A rolling reputation in [0,1] used as a *ranking* signal (not a hard gate):
    providers that have recently shipped good clips for similar shots rank first.
    The live per-clip gate is :class:`QualityGateProtocol`.
    """

    def reputation(self, provider: str) -> float:
        """Rolling quality reputation in [0,1] (higher = better)."""
        ...


@runtime_checkable
class QualityGateProtocol(Protocol):
    """The live per-clip quality gate (round-2 ``quality`` Critic seam, §10).

    Scores a freshly-produced clip against the shot's intent/canon. The
    coordinator rejects + escalates when the score is below ``shot.min_quality``
    — "a wrong face is a fail even if the scene is pretty".
    """

    async def score(self, shot: ShotSpec, result: RenderResult) -> float:
        """Return a quality score in [0,1] for ``result`` against ``shot``."""
        ...


@runtime_checkable
class CostBudgetProtocol(Protocol):
    """The video-second / dollar budget guardrail (round-1 ``cost``, §11).

    ``estimate`` prices a candidate before we try it (cheap pre-flight pruning);
    ``reserve`` atomically holds spend for an attempt and returns a handle the
    coordinator must later ``settle`` (charge the real cost) or ``release`` (undo
    on failure). ``remaining_usd`` is the live headroom.
    """

    def estimate(self, provider: str, shot: ShotSpec) -> float:
        """Estimated USD cost of rendering ``shot`` on ``provider``."""
        ...

    def remaining_usd(self) -> float:
        """Remaining spendable budget in USD (>= 0)."""
        ...

    def reserve(self, provider: str, amount_usd: float) -> BudgetReservation | None:
        """Hold ``amount_usd``; return a reservation, or None if it cannot fit."""
        ...


@runtime_checkable
class BudgetReservation(Protocol):
    """A held budget reservation (the handle returned by :meth:`CostBudgetProtocol.reserve`)."""

    @property
    def amount_usd(self) -> float:
        """The reserved amount."""
        ...

    def settle(self, actual_usd: float) -> None:
        """Commit the reservation, charging the actual cost (<= reserved)."""
        ...

    def release(self) -> None:
        """Return the reserved budget unspent (attempt failed before charging)."""
        ...


@runtime_checkable
class RouterProtocol(Protocol):
    """The provider router with failover + hedge (round-1 ``router``).

    The coordinator picks the *provider order* (via governor + reputation + cost)
    and then asks the router to actually attempt one provider, delegating the
    bounded retries / hedge / per-attempt failover *inside* that provider to the
    router. The router raises on exhaustion; the coordinator catches it and
    escalates to the next-best provider.
    """

    def candidates(self, shot: ShotSpec) -> list[str]:
        """All providers the router knows that *could* serve this shot."""
        ...

    async def render(self, provider: str, shot: ShotSpec) -> RenderResult:
        """Render ``shot`` on ``provider`` (with the router's own retries/hedge).

        Raises on failure (timeout / 5xx / exhausted retries / bad request).
        """
        ...


@runtime_checkable
class JobSinkProtocol(Protocol):
    """The async job-lifecycle sink (round-2 ``jobs``).

    A fire-and-observe seam so the coordinator can publish lifecycle transitions
    (started / attempting / accepted / degraded / failed) for an async render job
    without coupling to a concrete queue. All methods are best-effort and must
    never raise into the coordinator.
    """

    def started(self, shot: ShotSpec) -> None: ...

    def progress(self, shot: ShotSpec, event: str, fields: Mapping[str, object]) -> None: ...

    def finished(self, shot: ShotSpec, outcome_ok: bool, tier: int) -> None: ...


__all__ = [
    "BudgetReservation",
    "CostBudgetProtocol",
    "GovernorProtocol",
    "JobSinkProtocol",
    "QualityGateProtocol",
    "QualityReputationProtocol",
    "RouterProtocol",
]
