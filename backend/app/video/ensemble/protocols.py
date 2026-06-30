"""Local structural contracts for best-of-N rendering (no cross-round imports).

This module defines the *minimal* seams the ensemble renderer needs, as standalone
:class:`typing.Protocol` classes. Per the round constraint, nothing here imports the
provider/quality/budget packages from earlier rounds — but each protocol is shaped to
be **structurally compatible** with the real types so a production wiring is a no-op:

* :class:`EnsembleProvider` mirrors ``app.providers.video_router.VideoBackend`` — a
  ``name`` plus ``async render(spec)`` — so a real ``VideoProvider`` / ``VideoRouter``
  satisfies it directly. A render returns a :class:`~app.video.ensemble.models.RenderOutput`,
  a structural shim over the real ``VideoResult`` (we only read ``model`` + a clip ref).
* :class:`QualityScorer` mirrors the §9.5 Critic's four-axis judgement: it scores one
  candidate clip against a (locked) canon context and returns a normalized 0..1 score
  plus the per-axis sub-scores the selection objectives and consistency vote read.
* :class:`MultiRenderBudget` mirrors ``app.memory.budget_service.BudgetService`` —
  ``can_render_live`` / ``reserve`` / ``commit`` / ``release`` over video-seconds — so a
  real budget service slots in. The ensemble's *own* per-shot fan-out cap lives in
  :class:`~app.video.ensemble.budget_guard.MultiRenderBudgetGuard`, which wraps one of
  these; this protocol is only the underlying scarce-seconds ledger.

All protocols are :func:`runtime_checkable` so the renderer can defensively assert a
collaborator's shape in tests without importing a concrete class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import (
        BudgetReservation,
        QualityScore,
        RenderOutput,
        ShotRenderSpec,
    )


@runtime_checkable
class EnsembleProvider(Protocol):
    """One model/backend the ensemble can render a shot on.

    Structurally identical to ``app.providers.video_router.VideoBackend`` (a ``name``
    plus ``async render``), so a real ``VideoProvider`` or ``VideoRouter`` is a member
    with no adapter. ``render`` is expected to raise on a gated/failed render; the
    ensemble treats any exception as a losing candidate (it never fabricates a clip).
    """

    #: Stable identity for ordering, scoring attribution, telemetry, and tie-breaks.
    name: str

    async def render(self, spec: ShotRenderSpec) -> RenderOutput:
        """Render ``spec`` to a clip on this provider. May raise on failure."""
        ...


@runtime_checkable
class QualityScorer(Protocol):
    """Scores a rendered candidate against the §9.5 quality axes (0..1, higher better).

    Mirrors the Critic's four pre-registered checks (identity/style/timeline/motion)
    collapsed into one composite plus the per-axis breakdown. The ensemble never
    *interprets* the gate (that's the Critic's job) — it only ranks candidates by the
    composite score, and (in consistency-vote mode) by the identity axis.
    """

    async def score(self, output: RenderOutput, spec: ShotRenderSpec) -> QualityScore:
        """Return a normalized quality judgement for ``output`` of ``spec``."""
        ...


@runtime_checkable
class MultiRenderBudget(Protocol):
    """The scarce video-seconds ledger the fan-out draws against.

    Structurally compatible with ``app.memory.budget_service.BudgetService``: the
    ensemble reserves seconds before launching a candidate, commits on a win, and
    releases on a loss/cancel. ``can_render_live`` is the hard ``KINORA_LIVE_VIDEO``
    go-live gate — best-of-N is *additionally* gated on it (see the guard).
    """

    def can_render_live(self) -> bool:
        """The hard live-video go-live gate (``KINORA_LIVE_VIDEO``)."""
        ...

    async def reserve(
        self,
        video_seconds: float,
        *,
        book_id: str | None = ...,
        session_id: str | None = ...,
        scene_id: str | None = ...,
    ) -> BudgetReservation:
        """Earmark ``video_seconds``; raise if a cap would be breached."""
        ...

    async def commit(
        self,
        reservation: BudgetReservation,
        *,
        actual_seconds: float | None = ...,
    ) -> None:
        """Charge the (winning) reservation's seconds against the ledger."""
        ...

    async def release(self, reservation: BudgetReservation) -> None:
        """Return an unused earmark (a losing/cancelled candidate)."""
        ...


__all__ = [
    "EnsembleProvider",
    "MultiRenderBudget",
    "QualityScorer",
]
