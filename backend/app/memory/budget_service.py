"""Budget service — the hard video-seconds cap as a service (kinora.md §11.1).

Video is the scarce, metered resource. This service is the real guardrail:
``reserve`` earmarks seconds before a render and **raises** :class:`BudgetExceeded`
if it would breach the global ceiling, the per-session allocation, or the
per-scene allocation; ``commit`` charges the actual seconds; ``release`` returns
an earmark (cancellation / cache hit). It is backed by the append-only
:class:`app.db.repositories.budget.BudgetRepo` ledger, and a transaction-scoped
advisory lock serializes concurrent reservations so two cannot both slip past
the ceiling. ``can_render_live`` is the ``KINORA_LIVE_VIDEO`` go-live gate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.config import Settings
from app.db.base import new_id
from app.db.models.budget import BudgetKind
from app.db.repositories.budget import BudgetRepo

# A fixed 63-bit advisory-lock key for the budget domain (signed bigint).
_LOCK_KEY = int.from_bytes(hashlib.sha1(b"kinora:budget").digest()[:8], "big", signed=True)


class BudgetExceeded(RuntimeError):  # noqa: N818 - public name in task contract
    """Raised when a reservation would breach a budget cap."""

    def __init__(self, scope: str, *, requested: float, used: float, cap: float) -> None:
        self.scope = scope
        self.requested = requested
        self.used = used
        self.cap = cap
        super().__init__(
            f"budget {scope} cap exceeded: requested {requested:.1f}s "
            f"+ used {used:.1f}s > cap {cap:.1f}s"
        )


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """The caps and gate that parameterize the budget service."""

    ceiling_video_s: float
    per_session_s: float
    per_scene_s: float
    low_floor_s: float
    live_video: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetLimits:
        """Build limits from application :class:`Settings`."""
        return cls(
            ceiling_video_s=settings.budget_ceiling_video_s,
            per_session_s=settings.budget_per_session_s,
            per_scene_s=settings.budget_per_scene_s,
            low_floor_s=settings.budget_low_floor_s,
            live_video=settings.kinora_live_video,
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """A handle to an outstanding budget reservation."""

    id: str
    video_seconds: float
    book_id: str | None = None
    session_id: str | None = None
    scene_id: str | None = None


class BudgetService:
    """Reserve / commit / release video-seconds against a hard, persistent cap."""

    def __init__(self, *, repo: BudgetRepo, limits: BudgetLimits) -> None:
        self._repo = repo
        self._limits = limits

    @property
    def limits(self) -> BudgetLimits:
        """The active caps/gate (read-only)."""
        return self._limits

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        """Earmark ``video_seconds``; raise :class:`BudgetExceeded` if a cap is hit."""
        if video_seconds < 0:
            raise ValueError("video_seconds must be non-negative")

        # Serialize concurrent reservations on the budget domain (released at
        # transaction end) so the cap checks below are race-free.
        await self._repo.advisory_lock(_LOCK_KEY)

        used_global = await self._repo.used_seconds()
        if used_global + video_seconds > self._limits.ceiling_video_s:
            raise BudgetExceeded(
                "ceiling",
                requested=video_seconds,
                used=used_global,
                cap=self._limits.ceiling_video_s,
            )

        if session_id is not None:
            used_session = await self._repo.used_seconds(session_id=session_id)
            if used_session + video_seconds > self._limits.per_session_s:
                raise BudgetExceeded(
                    "session",
                    requested=video_seconds,
                    used=used_session,
                    cap=self._limits.per_session_s,
                )

        if scene_id is not None:
            used_scene = await self._repo.used_seconds(scene_id=scene_id)
            if used_scene + video_seconds > self._limits.per_scene_s:
                raise BudgetExceeded(
                    "scene", requested=video_seconds, used=used_scene, cap=self._limits.per_scene_s
                )

        reservation_id = new_id()
        await self._repo.append(
            kind=BudgetKind.RESERVE,
            video_seconds=video_seconds,
            reservation_id=reservation_id,
            entry_id=reservation_id,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
            note=note,
        )
        return Reservation(
            id=reservation_id,
            video_seconds=video_seconds,
            book_id=book_id,
            session_id=session_id,
            scene_id=scene_id,
        )

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        """Charge the actual seconds for a finished render (closes the reservation)."""
        seconds = reservation.video_seconds if actual_seconds is None else actual_seconds
        await self._repo.append(
            kind=BudgetKind.COMMIT,
            video_seconds=seconds,
            reservation_id=reservation.id,
            book_id=reservation.book_id,
            session_id=reservation.session_id,
            scene_id=reservation.scene_id,
            note=note,
        )

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        """Return an earmark (cancellation / cache hit); restores remaining."""
        await self._repo.append(
            kind=BudgetKind.RELEASE,
            video_seconds=0.0,
            reservation_id=reservation.id,
            book_id=reservation.book_id,
            session_id=reservation.session_id,
            scene_id=reservation.scene_id,
            note=note,
        )

    async def remaining(self) -> float:
        """Ceiling − committed − outstanding-reserved (global)."""
        used = await self._repo.used_seconds()
        return self._limits.ceiling_video_s - used

    async def is_low(self) -> bool:
        """True when remaining has dropped below the degradation floor (§11.1)."""
        return await self.remaining() < self._limits.low_floor_s

    def can_render_live(self) -> bool:
        """The ``KINORA_LIVE_VIDEO`` go-live gate (§11.1)."""
        return self._limits.live_video


__all__ = ["BudgetExceeded", "BudgetLimits", "BudgetService", "Reservation"]
