"""Read-only data seams — the narrow, typed views the builders read from.

Builders must never touch the ORM directly: they read **plain frozen
dataclasses** produced here. This keeps the builders pure + trivially testable
(hand a builder a dataclass, assert on the report) and keeps every database query
in one auditable place. Each ``*Source`` is a thin read-only repository over an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`; the dataclasses they return carry
*already-aggregated numbers*, never live rows.

Everything here is **read-only** — ``SELECT`` only, no writes, no mutation, zero
video-seconds. The aggregations mirror the data already computed by the budget
service (§11), the shot pipeline (§9), and the eval harness (§13); this module
only reads and shapes it for documents.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.models.defect import Defect
from app.db.models.enums import RenderJobStatus, ShotStatus
from app.db.models.render_job import RenderJob
from app.db.models.scene import Scene
from app.db.models.session import Session
from app.db.models.shot import Shot

# --------------------------------------------------------------------------- #
# Reader-facing aggregates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BookProgress:
    """A reader's progress against one book."""

    book_id: str
    title: str
    author: str | None
    num_pages: int
    status: str
    furthest_word: int
    total_words: int
    accepted_shots: int
    total_shots: int
    watched_seconds: float
    last_read_at: datetime | None

    @property
    def percent_complete(self) -> float:
        """Fraction (0–1) of the book reached, by furthest word / total words."""
        if self.total_words <= 0:
            return 0.0
        return min(1.0, self.furthest_word / self.total_words)

    @property
    def is_complete(self) -> bool:
        """A book counts complete at ≥ 98% read (a margin for trailing matter)."""
        return self.percent_complete >= 0.98


@dataclass(frozen=True, slots=True)
class ReaderSummary:
    """The whole-library rollup for one reader (year-in-review / digest)."""

    user_id: str
    books: tuple[BookProgress, ...]
    window_start: datetime | None = None
    window_end: datetime | None = None

    @property
    def books_started(self) -> int:
        return sum(1 for b in self.books if b.furthest_word > 0)

    @property
    def books_completed(self) -> int:
        return sum(1 for b in self.books if b.is_complete)

    @property
    def total_watched_seconds(self) -> float:
        return sum(b.watched_seconds for b in self.books)

    @property
    def total_accepted_shots(self) -> int:
        return sum(b.accepted_shots for b in self.books)

    @property
    def total_pages(self) -> int:
        return sum(b.num_pages for b in self.books if b.is_complete)


class ReaderSource:
    """Read-only reader aggregates (progress, library rollups)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _total_words(self, book_id: str) -> int:
        # Total words ≈ the max source-span end across the book's shots; the
        # source-span index is the §4.2 word→shot map, so its max end word is the
        # last word the book was decomposed over. Callers fall back to
        # pages × ~250 when the index is empty (a book that never finished Phase A).
        from app.db.models.shot import SourceSpanIndex

        max_word = await self.session.scalar(
            select(func.coalesce(func.max(SourceSpanIndex.word_index_end), 0)).where(
                SourceSpanIndex.book_id == book_id
            )
        )
        return int(max_word or 0)

    async def book_progress(self, book_id: str, user_id: str) -> BookProgress | None:
        """Progress for one owned book (None if not the user's / missing)."""
        book = await self.session.get(Book, book_id)
        if book is None or book.user_id != user_id:
            return None
        return await self._progress_for_book(book, user_id)

    async def _progress_for_book(self, book: Book, user_id: str) -> BookProgress:
        # Furthest word + last activity across the reader's sessions on this book.
        furthest = await self.session.scalar(
            select(func.coalesce(func.max(Session.focus_word), 0)).where(
                Session.book_id == book.id, Session.user_id == user_id
            )
        )
        last_ms = await self.session.scalar(
            select(func.max(Session.last_activity_ms)).where(
                Session.book_id == book.id, Session.user_id == user_id
            )
        )
        last_read = (
            datetime.fromtimestamp(last_ms / 1000.0, tz=UTC)
            if last_ms
            else None
        )
        total_shots = await self.session.scalar(
            select(func.count()).select_from(Shot).where(Shot.book_id == book.id)
        )
        accepted = await self.session.scalar(
            select(func.count())
            .select_from(Shot)
            .where(Shot.book_id == book.id, Shot.status == ShotStatus.ACCEPTED)
        )
        # Watched video-seconds = Σ accepted shot durations (a proxy for footage).
        watched = await self.session.scalar(
            select(func.coalesce(func.sum(Shot.duration_s), 0.0)).where(
                Shot.book_id == book.id, Shot.status == ShotStatus.ACCEPTED
            )
        )
        total_words = await self._total_words(book.id)
        if total_words <= 0 and book.num_pages:
            total_words = book.num_pages * 250
        return BookProgress(
            book_id=book.id,
            title=book.title,
            author=book.author,
            num_pages=int(book.num_pages or 0),
            status=str(book.status.value if hasattr(book.status, "value") else book.status),
            furthest_word=int(furthest or 0),
            total_words=int(total_words),
            accepted_shots=int(accepted or 0),
            total_shots=int(total_shots or 0),
            watched_seconds=float(watched or 0.0),
            last_read_at=last_read,
        )

    async def reader_summary(
        self,
        user_id: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> ReaderSummary:
        """Roll up every book the reader owns into one summary."""
        books = list(
            (
                await self.session.scalars(
                    select(Book).where(Book.user_id == user_id).order_by(Book.created_at)
                )
            ).all()
        )
        progress = tuple(
            [await self._progress_for_book(b, user_id) for b in books]
        )
        return ReaderSummary(
            user_id=user_id,
            books=progress,
            window_start=window_start,
            window_end=window_end,
        )


# --------------------------------------------------------------------------- #
# Operator-facing aggregates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """The §11 budget state, scoped (global or per-book)."""

    ceiling_seconds: float
    committed_seconds: float
    reserved_seconds: float
    reservation_count: int
    commit_count: int
    release_count: int
    by_book: tuple[tuple[str, float], ...] = ()

    @property
    def used_seconds(self) -> float:
        return self.committed_seconds + self.reserved_seconds

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.ceiling_seconds - self.used_seconds)

    @property
    def used_fraction(self) -> float:
        if self.ceiling_seconds <= 0:
            return 0.0
        return min(1.0, self.used_seconds / self.ceiling_seconds)


@dataclass(frozen=True, slots=True)
class QualitySnapshot:
    """The §13 quality numbers as recorded on accepted/failed shots + defects."""

    total_shots: int
    accepted_shots: int
    degraded_shots: int
    conflict_shots: int
    total_video_seconds: float
    accepted_video_seconds: float
    regen_count: int
    defect_count: int
    defects_by_kind: tuple[tuple[str, int], ...] = ()
    mean_ccs: float | None = None
    mean_critic_score: float | None = None

    @property
    def accepted_footage_efficiency(self) -> float:
        """``(accepted / total) × 100`` over video-seconds (§13 headline)."""
        if self.total_video_seconds <= 0:
            return 100.0
        return min(100.0, (self.accepted_video_seconds / self.total_video_seconds) * 100.0)

    @property
    def regeneration_rate(self) -> float:
        if self.total_shots <= 0:
            return 0.0
        return self.regen_count / self.total_shots


@dataclass(frozen=True, slots=True)
class ThroughputSnapshot:
    """Render-queue throughput + per-status job counts (§12)."""

    jobs_total: int
    by_status: tuple[tuple[str, int], ...]
    succeeded: int
    deadletter: int
    cancelled: int
    mean_attempts: float
    reserved_seconds_outstanding: float

    @property
    def success_rate(self) -> float:
        terminal = self.succeeded + self.deadletter + self.cancelled
        if terminal <= 0:
            return 0.0
        return self.succeeded / terminal


@dataclass(frozen=True, slots=True)
class SceneRow:
    """A per-scene operator line (spend + acceptance)."""

    scene_id: str
    title: str | None
    shots: int
    accepted: int
    video_seconds: float
    accepted_seconds: float


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """A fleet-level library overview (books by status, totals)."""

    total_books: int
    by_status: tuple[tuple[str, int], ...]
    total_shots: int
    accepted_shots: int
    total_users: int


class OperatorSource:
    """Read-only operator aggregates (budget / quality / throughput / library)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def budget_snapshot(
        self, *, ceiling_seconds: float, book_id: str | None = None
    ) -> BudgetSnapshot:
        """Aggregate the budget ledger (global or scoped to one book)."""
        base = select(BudgetLedger)
        if book_id is not None:
            base = base.where(BudgetLedger.book_id == book_id)
        rows = list((await self.session.scalars(base)).all())
        committed = sum(r.video_seconds for r in rows if r.kind == BudgetKind.COMMIT)
        # Outstanding reserved = reserves with no matching commit/release row.
        closed = {
            r.reservation_id
            for r in rows
            if r.kind in (BudgetKind.COMMIT, BudgetKind.RELEASE)
        }
        reserved = sum(
            r.video_seconds
            for r in rows
            if r.kind == BudgetKind.RESERVE and r.id not in closed
        )
        commit_count = sum(1 for r in rows if r.kind == BudgetKind.COMMIT)
        reserve_count = sum(1 for r in rows if r.kind == BudgetKind.RESERVE)
        release_count = sum(1 for r in rows if r.kind == BudgetKind.RELEASE)
        # Top books by committed seconds.
        by_book: dict[str, float] = {}
        for r in rows:
            if r.kind == BudgetKind.COMMIT and r.book_id:
                by_book[r.book_id] = by_book.get(r.book_id, 0.0) + r.video_seconds
        top = tuple(sorted(by_book.items(), key=lambda kv: -kv[1])[:10])
        return BudgetSnapshot(
            ceiling_seconds=ceiling_seconds,
            committed_seconds=float(committed),
            reserved_seconds=float(reserved),
            reservation_count=reserve_count,
            commit_count=commit_count,
            release_count=release_count,
            by_book=top,
        )

    async def quality_snapshot(self, *, book_id: str | None = None) -> QualitySnapshot:
        """Aggregate per-shot QA outcomes + defects (§13)."""
        stmt = select(Shot)
        if book_id is not None:
            stmt = stmt.where(Shot.book_id == book_id)
        shots = list((await self.session.scalars(stmt)).all())
        total = len(shots)
        accepted = sum(1 for s in shots if s.status == ShotStatus.ACCEPTED)
        degraded = sum(1 for s in shots if s.status == ShotStatus.DEGRADED)
        conflict = sum(1 for s in shots if s.status == ShotStatus.CONFLICT)
        total_secs = sum(float(s.duration_s or 0.0) for s in shots)
        accepted_secs = sum(
            float(s.duration_s or 0.0) for s in shots if s.status == ShotStatus.ACCEPTED
        )
        # Regens + scores pulled from the qa JSON when present.
        regens = 0
        ccs_vals: list[float] = []
        critic_vals: list[float] = []
        for s in shots:
            qa = s.qa or {}
            if isinstance(qa, dict):
                regens += int(qa.get("regens", 0) or 0)
                ccs = qa.get("ccs")
                if isinstance(ccs, (int, float)):
                    ccs_vals.append(float(ccs))
                score = qa.get("score") or qa.get("critic_score")
                if isinstance(score, (int, float)):
                    critic_vals.append(float(score))
        # Defects.
        dstmt = select(Defect)
        if book_id is not None:
            dstmt = dstmt.where(Defect.book_id == book_id)
        defects = list((await self.session.scalars(dstmt)).all())
        kinds: Counter[str] = Counter(d.kind for d in defects)
        return QualitySnapshot(
            total_shots=total,
            accepted_shots=accepted,
            degraded_shots=degraded,
            conflict_shots=conflict,
            total_video_seconds=total_secs,
            accepted_video_seconds=accepted_secs,
            regen_count=regens,
            defect_count=len(defects),
            defects_by_kind=tuple(sorted(kinds.items(), key=lambda kv: -kv[1])),
            mean_ccs=(sum(ccs_vals) / len(ccs_vals)) if ccs_vals else None,
            mean_critic_score=(sum(critic_vals) / len(critic_vals)) if critic_vals else None,
        )

    async def throughput_snapshot(self, *, book_id: str | None = None) -> ThroughputSnapshot:
        """Aggregate render-job statuses + attempts (§12)."""
        stmt = select(RenderJob)
        if book_id is not None:
            # Render jobs scope by shot's book; join via Shot.
            stmt = stmt.join(Shot, Shot.id == RenderJob.shot_id).where(Shot.book_id == book_id)
        jobs = list((await self.session.scalars(stmt)).all())
        by_status: Counter[str] = Counter(str(j.status.value) for j in jobs)
        succeeded = by_status.get(RenderJobStatus.SUCCEEDED.value, 0)
        deadletter = by_status.get(RenderJobStatus.DEADLETTER.value, 0)
        cancelled = by_status.get(RenderJobStatus.CANCELLED.value, 0)
        attempts = [j.attempts for j in jobs]
        mean_attempts = (sum(attempts) / len(attempts)) if attempts else 0.0
        reserved = sum(
            float(j.reserved_video_s or 0.0)
            for j in jobs
            if j.status
            not in (
                RenderJobStatus.SUCCEEDED,
                RenderJobStatus.CANCELLED,
                RenderJobStatus.DEADLETTER,
            )
        )
        return ThroughputSnapshot(
            jobs_total=len(jobs),
            by_status=tuple(sorted(by_status.items(), key=lambda kv: -kv[1])),
            succeeded=succeeded,
            deadletter=deadletter,
            cancelled=cancelled,
            mean_attempts=mean_attempts,
            reserved_seconds_outstanding=reserved,
        )

    async def scene_rows(self, book_id: str) -> tuple[SceneRow, ...]:
        """Per-scene spend + acceptance for a book (operator drill-down)."""
        scenes = {
            s.id: s
            for s in (
                await self.session.scalars(select(Scene).where(Scene.book_id == book_id))
            ).all()
        }
        shots = list(
            (await self.session.scalars(select(Shot).where(Shot.book_id == book_id))).all()
        )
        by_scene: dict[str, list[Shot]] = {}
        for s in shots:
            by_scene.setdefault(s.scene_id or "—", []).append(s)
        rows: list[SceneRow] = []
        for scene_id, group in by_scene.items():
            scene = scenes.get(scene_id)
            rows.append(
                SceneRow(
                    scene_id=scene_id,
                    title=getattr(scene, "title", None) if scene else None,
                    shots=len(group),
                    accepted=sum(1 for s in group if s.status == ShotStatus.ACCEPTED),
                    video_seconds=sum(float(s.duration_s or 0.0) for s in group),
                    accepted_seconds=sum(
                        float(s.duration_s or 0.0)
                        for s in group
                        if s.status == ShotStatus.ACCEPTED
                    ),
                )
            )
        rows.sort(key=lambda r: r.scene_id)
        return tuple(rows)

    async def library_snapshot(self) -> LibrarySnapshot:
        """Fleet-level book counts by status + totals."""
        books = list((await self.session.scalars(select(Book))).all())
        by_status: Counter[str] = Counter(
            str(b.status.value if hasattr(b.status, "value") else b.status) for b in books
        )
        total_shots = await self.session.scalar(select(func.count()).select_from(Shot)) or 0
        accepted = (
            await self.session.scalar(
                select(func.count())
                .select_from(Shot)
                .where(Shot.status == ShotStatus.ACCEPTED)
            )
            or 0
        )
        users = await self.session.scalar(
            select(func.count(func.distinct(Book.user_id)))
        ) or 0
        return LibrarySnapshot(
            total_books=len(books),
            by_status=tuple(sorted(by_status.items(), key=lambda kv: -kv[1])),
            total_shots=int(total_shots),
            accepted_shots=int(accepted),
            total_users=int(users),
        )


__all__ = [
    "BookProgress",
    "BudgetSnapshot",
    "LibrarySnapshot",
    "OperatorSource",
    "QualitySnapshot",
    "ReaderSource",
    "ReaderSummary",
    "SceneRow",
    "ThroughputSnapshot",
]
