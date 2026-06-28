"""Budget administration + reporting (kinora.md §11.1, §13).

Video-seconds are the scarce, hard-capped currency. These actions read the
append-only ledger through the same :class:`BudgetService` the render path uses,
so the numbers an operator sees are exactly the numbers the cap enforces:

* ``report``     — global committed / outstanding-reserved / remaining vs. the
  ceiling, plus a per-book breakdown of committed seconds.
* ``remaining``  — the single headline number + the low-floor gate state.
* ``ledger``     — the most-recent ledger rows (the audit tail).
* ``caps``       — the configured ceiling / per-session / per-scene / floor.
* ``efficiency`` — the §13 accepted-footage metric: QA-passed seconds per 100s
  of generation budget, derived from committed-vs-accepted shots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.cli.formatting import humanize_seconds, isoformat, pct, truncate
from app.cli.output import Payload, Table, kv_table
from app.composition import Container
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.budget import BudgetRepo
from app.memory.budget_service import BudgetLimits, BudgetService


@dataclass(frozen=True, slots=True)
class PerBookSpend:
    """Committed video-seconds for one book."""

    book_id: str
    title: str | None
    committed_s: float


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """The result of ``budget report`` — global accounting + per-book breakdown."""

    ceiling_s: float
    committed_s: float
    reserved_s: float
    remaining_s: float
    low_floor_s: float
    is_low: bool
    live_video: bool
    per_book: tuple[PerBookSpend, ...] = field(default_factory=tuple)

    @property
    def used_s(self) -> float:
        return self.committed_s + self.reserved_s

    def render_payload(self) -> Payload:
        data = {
            "ceiling_s": self.ceiling_s,
            "committed_s": self.committed_s,
            "reserved_s": self.reserved_s,
            "used_s": self.used_s,
            "remaining_s": self.remaining_s,
            "remaining_pct": (self.remaining_s / self.ceiling_s) if self.ceiling_s else None,
            "low_floor_s": self.low_floor_s,
            "is_low": self.is_low,
            "live_video": self.live_video,
            "per_book": [
                {"book_id": b.book_id, "title": b.title, "committed_s": b.committed_s}
                for b in self.per_book
            ],
        }
        summary = kv_table(
            "budget (global, video-seconds)",
            {
                "ceiling": humanize_seconds(self.ceiling_s),
                "committed": humanize_seconds(self.committed_s),
                "reserved (outstanding)": humanize_seconds(self.reserved_s),
                "used": f"{humanize_seconds(self.used_s)} ({pct(self.used_s, self.ceiling_s)})",
                "remaining": (
                    f"{humanize_seconds(self.remaining_s)} "
                    f"({pct(self.remaining_s, self.ceiling_s)})"
                ),
                "low_floor": humanize_seconds(self.low_floor_s),
                "is_low": "YES" if self.is_low else "no",
                "live_video": "on" if self.live_video else "off",
            },
        )
        per_book = Table(
            title="committed by book (top spenders)",
            columns=("book_id", "title", "committed"),
            rows=[
                (
                    b.book_id,
                    truncate(b.title, 36) if b.title else "-",
                    humanize_seconds(b.committed_s),
                )
                for b in self.per_book
            ],
        )
        return Payload.of(data, summary, per_book)


@dataclass(frozen=True, slots=True)
class RemainingReport:
    """The result of ``budget remaining`` — one headline number + gate state."""

    remaining_s: float
    ceiling_s: float
    is_low: bool
    live_video: bool

    def render_payload(self) -> Payload:
        data = {
            "remaining_s": self.remaining_s,
            "ceiling_s": self.ceiling_s,
            "remaining_pct": (self.remaining_s / self.ceiling_s) if self.ceiling_s else None,
            "is_low": self.is_low,
            "live_video": self.live_video,
        }
        table = kv_table(
            "budget remaining",
            {
                "remaining": f"{humanize_seconds(self.remaining_s)} of "
                f"{humanize_seconds(self.ceiling_s)} ({pct(self.remaining_s, self.ceiling_s)})",
                "is_low": "YES" if self.is_low else "no",
                "live_video": "on" if self.live_video else "off",
            },
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One ledger row for the audit tail."""

    id: str
    kind: str
    video_seconds: float
    reservation_id: str
    book_id: str | None
    session_id: str | None
    scene_id: str | None
    note: str | None
    created_at_iso: str | None


@dataclass(frozen=True, slots=True)
class LedgerTail:
    """The result of ``budget ledger`` — most-recent ledger rows."""

    entries: tuple[LedgerEntry, ...]
    scope: dict[str, str | None]

    def render_payload(self) -> Payload:
        data = {
            "scope": self.scope,
            "entries": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "video_seconds": e.video_seconds,
                    "reservation_id": e.reservation_id,
                    "book_id": e.book_id,
                    "session_id": e.session_id,
                    "scene_id": e.scene_id,
                    "note": e.note,
                    "created_at": e.created_at_iso,
                }
                for e in self.entries
            ],
        }
        table = Table(
            title=f"budget ledger tail ({len(self.entries)})",
            columns=("kind", "seconds", "reservation", "book", "session", "note"),
            rows=[
                (
                    e.kind,
                    humanize_seconds(e.video_seconds),
                    truncate(e.reservation_id, 12),
                    truncate(e.book_id, 12) if e.book_id else "-",
                    truncate(e.session_id, 12) if e.session_id else "-",
                    truncate(e.note, 24) if e.note else "-",
                )
                for e in self.entries
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class CapsReport:
    """The result of ``budget caps`` — the configured limits."""

    limits: BudgetLimits

    def render_payload(self) -> Payload:
        ll = self.limits
        data = {
            "ceiling_video_s": ll.ceiling_video_s,
            "per_session_s": ll.per_session_s,
            "per_scene_s": ll.per_scene_s,
            "low_floor_s": ll.low_floor_s,
            "live_video": ll.live_video,
        }
        table = kv_table(
            "budget caps",
            {
                "ceiling_video": humanize_seconds(ll.ceiling_video_s),
                "per_session": humanize_seconds(ll.per_session_s),
                "per_scene": humanize_seconds(ll.per_scene_s),
                "low_floor": humanize_seconds(ll.low_floor_s),
                "live_video": "on" if ll.live_video else "off",
            },
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class EfficiencyReport:
    """The §13 accepted-footage efficiency metric, optionally scoped to a book."""

    book_id: str | None
    accepted_seconds: float
    committed_seconds: float
    accepted_shots: int
    total_committed_shots: int

    @property
    def efficiency_pct(self) -> float | None:
        """``(1 - rejected/total) * 100`` — QA-passed seconds per 100s budget."""
        if self.committed_seconds <= 0:
            return None
        return (self.accepted_seconds / self.committed_seconds) * 100.0

    def render_payload(self) -> Payload:
        eff = self.efficiency_pct
        data = {
            "book_id": self.book_id,
            "accepted_seconds": self.accepted_seconds,
            "committed_seconds": self.committed_seconds,
            "accepted_shots": self.accepted_shots,
            "total_committed_shots": self.total_committed_shots,
            "efficiency_pct": eff,
        }
        table = kv_table(
            "accepted-footage efficiency (§13)"
            + (f" — book {self.book_id}" if self.book_id else " — global"),
            {
                "accepted_footage": humanize_seconds(self.accepted_seconds),
                "committed_footage": humanize_seconds(self.committed_seconds),
                "accepted_shots": self.accepted_shots,
                "committed_shots": self.total_committed_shots,
                "efficiency": f"{eff:.1f}%" if eff is not None else "-",
            },
        )
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


def _service(container: Container, db: object) -> BudgetService:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db, AsyncSession)
    return BudgetService(repo=BudgetRepo(db), limits=container.budget_limits)


async def budget_report(container: Container, *, top: int = 10) -> BudgetReport:
    """Global accounting + the top-``top`` books by committed seconds."""
    limits = container.budget_limits
    async with container.session_factory() as db:
        repo = BudgetRepo(db)
        committed = await repo.committed_seconds()
        reserved = await repo.outstanding_reserved_seconds()
        remaining = limits.ceiling_video_s - (committed + reserved)

        rows = (
            await db.execute(
                select(
                    BudgetLedger.book_id,
                    func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0),
                )
                .where(
                    BudgetLedger.kind == BudgetKind.COMMIT,
                    BudgetLedger.book_id.is_not(None),
                )
                .group_by(BudgetLedger.book_id)
                .order_by(func.sum(BudgetLedger.video_seconds).desc())
                .limit(top)
            )
        ).all()
        from app.db.models.book import Book

        titles: dict[str, str] = {}
        book_ids = [r[0] for r in rows if r[0]]
        if book_ids:
            for book in (await db.execute(select(Book).where(Book.id.in_(book_ids)))).scalars():
                titles[book.id] = book.title

    service_limits = limits
    per_book = tuple(
        PerBookSpend(book_id=bid, title=titles.get(bid), committed_s=float(secs))
        for bid, secs in rows
        if bid
    )
    return BudgetReport(
        ceiling_s=service_limits.ceiling_video_s,
        committed_s=committed,
        reserved_s=reserved,
        remaining_s=remaining,
        low_floor_s=service_limits.low_floor_s,
        is_low=remaining < service_limits.low_floor_s,
        live_video=service_limits.live_video,
        per_book=per_book,
    )


async def budget_remaining(container: Container) -> RemainingReport:
    """The single remaining-seconds number + gate state."""
    limits = container.budget_limits
    async with container.session_factory() as db:
        service = _service(container, db)
        remaining = await service.remaining()
    return RemainingReport(
        remaining_s=remaining,
        ceiling_s=limits.ceiling_video_s,
        is_low=remaining < limits.low_floor_s,
        live_video=limits.live_video,
    )


async def budget_ledger(
    container: Container,
    *,
    book_id: str | None = None,
    session_id: str | None = None,
    scene_id: str | None = None,
    limit: int = 50,
) -> LedgerTail:
    """The most-recent ledger rows, newest first, optionally scope-filtered."""
    async with container.session_factory() as db:
        stmt = select(BudgetLedger).order_by(BudgetLedger.created_at.desc())
        if book_id is not None:
            stmt = stmt.where(BudgetLedger.book_id == book_id)
        if session_id is not None:
            stmt = stmt.where(BudgetLedger.session_id == session_id)
        if scene_id is not None:
            stmt = stmt.where(BudgetLedger.scene_id == scene_id)
        stmt = stmt.limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
    entries = tuple(
        LedgerEntry(
            id=r.id,
            kind=r.kind.value,
            video_seconds=r.video_seconds,
            reservation_id=r.reservation_id,
            book_id=r.book_id,
            session_id=r.session_id,
            scene_id=r.scene_id,
            note=r.note,
            created_at_iso=isoformat(r.created_at),
        )
        for r in rows
    )
    return LedgerTail(
        entries=entries,
        scope={"book_id": book_id, "session_id": session_id, "scene_id": scene_id},
    )


def budget_caps(container: Container) -> CapsReport:
    """Report the configured caps (pure — no DB)."""
    return CapsReport(limits=container.budget_limits)


async def budget_efficiency(
    container: Container, *, book_id: str | None = None
) -> EfficiencyReport:
    """The §13 accepted-footage efficiency, derived from committed budget + shots.

    Accepted footage is the duration of QA-passed (``accepted``) shots; committed
    footage is the total committed video-seconds in scope. The ratio is the
    headline efficiency number (minutes of consistent film per budget).
    """
    async with container.session_factory() as db:
        committed_stmt = select(func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0)).where(
            BudgetLedger.kind == BudgetKind.COMMIT
        )
        if book_id is not None:
            committed_stmt = committed_stmt.where(BudgetLedger.book_id == book_id)
        committed_seconds = float((await db.execute(committed_stmt)).scalar_one())

        accepted_stmt = select(func.coalesce(func.sum(Shot.duration_s), 0.0), func.count()).where(
            Shot.status == ShotStatus.ACCEPTED
        )
        if book_id is not None:
            accepted_stmt = accepted_stmt.where(Shot.book_id == book_id)
        accepted_row = (await db.execute(accepted_stmt)).one()
        accepted_seconds = float(accepted_row[0] or 0.0)
        accepted_shots = int(accepted_row[1] or 0)

        committed_shots_stmt = select(func.count()).select_from(Shot)
        if book_id is not None:
            committed_shots_stmt = committed_shots_stmt.where(Shot.book_id == book_id)
        total_committed_shots = int((await db.execute(committed_shots_stmt)).scalar_one())

    return EfficiencyReport(
        book_id=book_id,
        accepted_seconds=accepted_seconds,
        committed_seconds=committed_seconds,
        accepted_shots=accepted_shots,
        total_committed_shots=total_committed_shots,
    )


__all__ = [
    "BudgetReport",
    "CapsReport",
    "EfficiencyReport",
    "LedgerEntry",
    "LedgerTail",
    "PerBookSpend",
    "RemainingReport",
    "budget_caps",
    "budget_efficiency",
    "budget_ledger",
    "budget_remaining",
    "budget_report",
]
