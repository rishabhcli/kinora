"""Book-lifecycle actions: list / inspect / set-status / reingest / delete.

These are the operator's window onto §5.1 books and the §8.2 episodic store: a
shelf overview, a deep inspect (page/scene/shot/defect counts + budget spent for
the book), a manual status transition, a durable re-ingest (which leans on the
same persisted ``source_pdf_key`` the recovery worker uses), and a hard delete
that drops the row (cascading pages/scenes/shots/defects) and clears its object
storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.cli.errors import conflict, not_found
from app.cli.formatting import ago, humanize_seconds, isoformat, truncate
from app.cli.output import Payload, Table, kv_table
from app.composition import Container
from app.db.models.book import Book, Page
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.models.defect import Defect
from app.db.models.enums import BookStatus
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.db.repositories.book import BookRepo


@dataclass(frozen=True, slots=True)
class BookRow:
    """One shelf row for the list view."""

    id: str
    title: str
    author: str | None
    status: str
    num_pages: int | None
    user_id: str | None
    created_at_iso: str | None
    created_ago: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "status": self.status,
            "num_pages": self.num_pages,
            "user_id": self.user_id,
            "created_at": self.created_at_iso,
        }


@dataclass(frozen=True, slots=True)
class BookList:
    """The result of ``books list``."""

    books: tuple[BookRow, ...]
    total: int
    status_filter: str | None = None

    def render_payload(self) -> Payload:
        data = {
            "total": self.total,
            "status_filter": self.status_filter,
            "books": [b.as_dict() for b in self.books],
        }
        table = Table(
            title=f"books ({self.total})"
            + (f" — status={self.status_filter}" if self.status_filter else ""),
            columns=("id", "title", "status", "pages", "owner", "created"),
            rows=[
                (
                    b.id,
                    truncate(b.title, 40),
                    b.status,
                    str(b.num_pages) if b.num_pages is not None else "-",
                    truncate(b.user_id, 12) if b.user_id else "-",
                    b.created_ago,
                )
                for b in self.books
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class BookDetail:
    """The result of ``books inspect``."""

    id: str
    title: str
    author: str | None
    status: str
    num_pages: int | None
    user_id: str | None
    source_pdf_key: str | None
    cover_key: str | None
    created_at_iso: str | None
    page_count: int
    scene_count: int
    shot_count: int
    shots_accepted: int
    defect_count: int
    budget_committed_s: float
    budget_reserved_s: float
    shot_status_breakdown: dict[str, int] = field(default_factory=dict)

    def render_payload(self) -> Payload:
        data = {
            "id": self.id,
            "title": self.title,
            "author": self.author,
            "status": self.status,
            "num_pages": self.num_pages,
            "user_id": self.user_id,
            "source_pdf_key": self.source_pdf_key,
            "cover_key": self.cover_key,
            "created_at": self.created_at_iso,
            "counts": {
                "pages": self.page_count,
                "scenes": self.scene_count,
                "shots": self.shot_count,
                "shots_accepted": self.shots_accepted,
                "defects": self.defect_count,
            },
            "shot_status_breakdown": self.shot_status_breakdown,
            "budget": {
                "committed_s": self.budget_committed_s,
                "reserved_s": self.budget_reserved_s,
            },
        }
        info = kv_table(
            f"book {self.id}",
            {
                "title": self.title,
                "author": self.author or "-",
                "status": self.status,
                "owner": self.user_id or "-",
                "num_pages": self.num_pages if self.num_pages is not None else "-",
                "source_pdf_key": self.source_pdf_key or "-",
                "cover_key": self.cover_key or "-",
                "created_at": self.created_at_iso or "-",
                "pages": self.page_count,
                "scenes": self.scene_count,
                "shots": f"{self.shot_count} ({self.shots_accepted} accepted)",
                "defects": self.defect_count,
                "budget_committed": humanize_seconds(self.budget_committed_s),
                "budget_reserved": humanize_seconds(self.budget_reserved_s),
            },
        )
        breakdown = Table(
            title="shot status breakdown",
            columns=("status", "count"),
            rows=[(k, str(v)) for k, v in sorted(self.shot_status_breakdown.items())],
        )
        return Payload.of(data, info, breakdown)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """A generic "did a thing" result with a short message + structured fields."""

    ok: bool
    action: str
    detail: dict[str, object]
    message: str

    def render_payload(self) -> Payload:
        data = {"ok": self.ok, "action": self.action, **self.detail}
        return Payload.of(data, kv_table(self.action, {"result": self.message, **self.detail}))


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def list_books(
    container: Container, *, status: BookStatus | None = None, limit: int = 100
) -> BookList:
    """Return the shelf (optionally filtered by status), newest first."""
    async with container.session_factory() as db:
        stmt = select(Book).order_by(Book.created_at.desc())
        if status is not None:
            stmt = stmt.where(Book.status == status)
        stmt = stmt.limit(limit)
        rows = list((await db.execute(stmt)).scalars().all())
        total_stmt = select(func.count()).select_from(Book)
        if status is not None:
            total_stmt = total_stmt.where(Book.status == status)
        total = int((await db.execute(total_stmt)).scalar_one())
    books = tuple(
        BookRow(
            id=b.id,
            title=b.title,
            author=b.author,
            status=b.status.value,
            num_pages=b.num_pages,
            user_id=b.user_id,
            created_at_iso=isoformat(b.created_at),
            created_ago=ago(b.created_at),
        )
        for b in rows
    )
    return BookList(books=books, total=total, status_filter=status.value if status else None)


async def inspect_book(container: Container, book_id: str) -> BookDetail:
    """Deep-inspect one book: counts, shot-status breakdown, budget spent."""
    async with container.session_factory() as db:
        book = await db.get(Book, book_id)
        if book is None:
            raise not_found("book", book_id)

        page_count = int(
            (
                await db.execute(
                    select(func.count()).select_from(Page).where(Page.book_id == book_id)
                )
            ).scalar_one()
        )
        scene_count = int(
            (
                await db.execute(
                    select(func.count()).select_from(Scene).where(Scene.book_id == book_id)
                )
            ).scalar_one()
        )
        defect_count = int(
            (
                await db.execute(
                    select(func.count()).select_from(Defect).where(Defect.book_id == book_id)
                )
            ).scalar_one()
        )
        status_rows = (
            await db.execute(
                select(Shot.status, func.count())
                .where(Shot.book_id == book_id)
                .group_by(Shot.status)
            )
        ).all()
        breakdown: dict[str, int] = {status.value: int(count) for status, count in status_rows}
        shot_count = sum(breakdown.values())
        shots_accepted = breakdown.get("accepted", 0)

        committed = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0)).where(
                        BudgetLedger.book_id == book_id,
                        BudgetLedger.kind == BudgetKind.COMMIT,
                    )
                )
            ).scalar_one()
        )
        # Outstanding reserved = reserve rows for this book with no commit/release.
        closed = select(BudgetLedger.reservation_id).where(
            BudgetLedger.kind.in_((BudgetKind.COMMIT, BudgetKind.RELEASE))
        )
        reserved = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(BudgetLedger.video_seconds), 0.0)).where(
                        BudgetLedger.book_id == book_id,
                        BudgetLedger.kind == BudgetKind.RESERVE,
                        BudgetLedger.reservation_id.not_in(closed),
                    )
                )
            ).scalar_one()
        )

        return BookDetail(
            id=book.id,
            title=book.title,
            author=book.author,
            status=book.status.value,
            num_pages=book.num_pages,
            user_id=book.user_id,
            source_pdf_key=book.source_pdf_key,
            cover_key=book.cover_key,
            created_at_iso=isoformat(book.created_at),
            page_count=page_count,
            scene_count=scene_count,
            shot_count=shot_count,
            shots_accepted=shots_accepted,
            defect_count=defect_count,
            budget_committed_s=committed,
            budget_reserved_s=reserved,
            shot_status_breakdown=breakdown,
        )


async def set_book_status(container: Container, book_id: str, status: BookStatus) -> ActionResult:
    """Transition a book's import status manually (recovery / unstick)."""
    async with container.session_factory() as db:
        repo = BookRepo(db)
        book = await repo.get(book_id)
        if book is None:
            raise not_found("book", book_id)
        previous = book.status.value
        await repo.set_status(book_id, status)
    return ActionResult(
        ok=True,
        action="set-status",
        detail={"book_id": book_id, "from": previous, "to": status.value},
        message=f"{book_id}: {previous} -> {status.value}",
    )


async def reingest_book(
    container: Container, book_id: str, *, reset_status: bool = True
) -> ActionResult:
    """Re-run Phase-A ingest for a book from its persisted source PDF.

    Reuses the container's single-flight ingest path (so a concurrent ingest is
    skipped, never doubled). Requires the book to have a ``source_pdf_key``;
    optionally flips it back to ``importing`` first so the UI reflects the rerun.
    """
    async with container.session_factory() as db:
        repo = BookRepo(db)
        book = await repo.get(book_id)
        if book is None:
            raise not_found("book", book_id)
        if not book.source_pdf_key:
            raise conflict(f"book {book_id} has no source_pdf_key; cannot re-ingest")
        pdf_key = book.source_pdf_key
        if reset_status and book.status is not BookStatus.IMPORTING:
            await repo.set_status(book_id, BookStatus.IMPORTING)

    import anyio

    pdf_bytes = await anyio.to_thread.run_sync(container.object_store.get_bytes, pdf_key)
    await container.run_ingest(book_id, pdf_bytes, None)
    return ActionResult(
        ok=True,
        action="reingest",
        detail={"book_id": book_id, "source_pdf_key": pdf_key, "bytes": len(pdf_bytes)},
        message=f"re-ingested {book_id} from {pdf_key}",
    )


async def delete_book(
    container: Container, book_id: str, *, purge_storage: bool = True
) -> ActionResult:
    """Hard-delete a book row (cascading children) and optionally purge its blobs.

    The row delete cascades pages/scenes/shots/defects (FK ``ON DELETE CASCADE``)
    and SET-NULLs the budget ledger so global accounting survives. Object-storage
    cleanup is best-effort and reported separately so a storage hiccup never
    blocks the DB delete.
    """
    from app.storage.object_store import keys

    async with container.session_factory() as db:
        repo = BookRepo(db)
        book = await repo.get(book_id)
        if book is None:
            raise not_found("book", book_id)
        source_key = book.source_pdf_key
        cover_key = book.cover_key
        await db.delete(book)

    purged: list[str] = []
    purge_errors = 0
    if purge_storage:
        import anyio

        candidate_keys = [k for k in (source_key, cover_key, keys.cover(book_id)) if k]
        for key in candidate_keys:
            try:
                if await anyio.to_thread.run_sync(container.object_store.exists, key):
                    await anyio.to_thread.run_sync(container.object_store.delete, key)
                    purged.append(key)
            except Exception:  # noqa: BLE001 - storage cleanup is best-effort
                purge_errors += 1

    return ActionResult(
        ok=True,
        action="delete",
        detail={
            "book_id": book_id,
            "purged_keys": purged,
            "purge_errors": purge_errors,
        },
        message=f"deleted {book_id} ({len(purged)} blob(s) purged, {purge_errors} error(s))",
    )


__all__ = [
    "ActionResult",
    "BookDetail",
    "BookList",
    "BookRow",
    "delete_book",
    "inspect_book",
    "list_books",
    "reingest_book",
    "set_book_status",
]
