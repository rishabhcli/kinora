"""User / tenant administration (kinora.md §5.1, §12 authz).

Books carry a durable ``user_id`` owner — the authoritative authz source. These
actions let an operator see who owns what, find an account by email, reassign a
book's owner (support / account-merge), and surface orphaned books (``user_id IS
NULL`` — owned by nobody, which fails closed in the API). There is no tenants
table yet; "tenant" administration is per-user ownership until one lands (see
DESIGN.md roadmap).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from app.cli.errors import not_found, usage
from app.cli.formatting import ago, isoformat, truncate
from app.cli.output import Payload, Table, kv_table
from app.composition import Container
from app.db.models.book import Book
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.db.repositories.user import UserRepo


@dataclass(frozen=True, slots=True)
class UserRow:
    id: str
    email: str
    book_count: int
    created_at_iso: str | None
    created_ago: str


@dataclass(frozen=True, slots=True)
class UserList:
    """The result of ``users list``."""

    users: tuple[UserRow, ...]
    total: int

    def render_payload(self) -> Payload:
        data = {
            "total": self.total,
            "users": [
                {
                    "id": u.id,
                    "email": u.email,
                    "book_count": u.book_count,
                    "created_at": u.created_at_iso,
                }
                for u in self.users
            ],
        }
        table = Table(
            title=f"users ({self.total})",
            columns=("id", "email", "books", "created"),
            rows=[
                (u.id, truncate(u.email, 32), str(u.book_count), u.created_ago) for u in self.users
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class UserDetail:
    """The result of ``users inspect`` / ``users find``."""

    id: str
    email: str
    created_at_iso: str | None
    book_count: int
    books: tuple[tuple[str, str, str], ...]  # (id, title, status)

    def render_payload(self) -> Payload:
        data = {
            "id": self.id,
            "email": self.email,
            "created_at": self.created_at_iso,
            "book_count": self.book_count,
            "books": [
                {"id": bid, "title": title, "status": status} for bid, title, status in self.books
            ],
        }
        info = kv_table(
            f"user {self.id}",
            {
                "email": self.email,
                "created_at": self.created_at_iso or "-",
                "book_count": self.book_count,
            },
        )
        books = Table(
            title="owned books",
            columns=("id", "title", "status"),
            rows=[(bid, truncate(title, 40), status) for bid, title, status in self.books],
        )
        return Payload.of(data, info, books)


@dataclass(frozen=True, slots=True)
class OrphanReport:
    """The result of ``users orphans`` — books with no owner."""

    books: tuple[tuple[str, str, str], ...]  # (id, title, status)
    total: int

    def render_payload(self) -> Payload:
        data = {
            "total": self.total,
            "books": [
                {"id": bid, "title": title, "status": status} for bid, title, status in self.books
            ],
        }
        table = Table(
            title=f"orphaned books (user_id IS NULL) ({self.total})",
            columns=("id", "title", "status"),
            rows=[(bid, truncate(title, 40), status) for bid, title, status in self.books],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class ReassignResult:
    """The result of ``users reassign``."""

    book_id: str
    from_user: str | None
    to_user: str

    def render_payload(self) -> Payload:
        data = {"book_id": self.book_id, "from_user": self.from_user, "to_user": self.to_user}
        table = kv_table(
            "reassign owner",
            {
                "book_id": self.book_id,
                "from": self.from_user or "(orphan)",
                "to": self.to_user,
            },
        )
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def _book_counts(db: object) -> dict[str, int]:
    rows = (
        await db.execute(  # type: ignore[attr-defined]
            select(Book.user_id, func.count())
            .where(Book.user_id.is_not(None))
            .group_by(Book.user_id)
        )
    ).all()
    return {uid: int(count) for uid, count in rows if uid}


async def list_users(container: Container, *, limit: int = 100) -> UserList:
    """List user accounts (newest first) with their owned-book counts."""
    async with container.session_factory() as db:
        rows = list(
            (await db.execute(select(User).order_by(User.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        total = int((await db.execute(select(func.count()).select_from(User))).scalar_one())
        counts = await _book_counts(db)
    users = tuple(
        UserRow(
            id=u.id,
            email=u.email,
            book_count=counts.get(u.id, 0),
            created_at_iso=isoformat(u.created_at),
            created_ago=ago(u.created_at),
        )
        for u in rows
    )
    return UserList(users=users, total=total)


async def inspect_user(
    container: Container, *, user_id: str | None = None, email: str | None = None
) -> UserDetail:
    """Inspect one user by id or email, listing the books they own."""
    if not user_id and not email:
        raise usage("provide --id or --email")
    async with container.session_factory() as db:
        repo = UserRepo(db)
        user = await repo.get(user_id) if user_id else await repo.get_by_email(email or "")
        if user is None:
            raise not_found("user", user_id or email or "?")
        books = await BookRepo(db).list_for_user(user.id)
    return UserDetail(
        id=user.id,
        email=user.email,
        created_at_iso=isoformat(user.created_at),
        book_count=len(books),
        books=tuple((b.id, b.title, b.status.value) for b in books),
    )


async def list_orphan_books(container: Container, *, limit: int = 200) -> OrphanReport:
    """List books with no durable owner (``user_id IS NULL``)."""
    async with container.session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Book)
                    .where(Book.user_id.is_(None))
                    .order_by(Book.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = int(
            (
                await db.execute(
                    select(func.count()).select_from(Book).where(Book.user_id.is_(None))
                )
            ).scalar_one()
        )
    return OrphanReport(
        books=tuple((b.id, b.title, b.status.value) for b in rows),
        total=total,
    )


async def reassign_book(container: Container, book_id: str, to_user_id: str) -> ReassignResult:
    """Reassign a book's durable owner to ``to_user_id`` (must exist)."""
    async with container.session_factory() as db:
        book = await db.get(Book, book_id)
        if book is None:
            raise not_found("book", book_id)
        target = await UserRepo(db).get(to_user_id)
        if target is None:
            raise not_found("user", to_user_id)
        previous = book.user_id
        book.user_id = to_user_id
        await db.flush()
    return ReassignResult(book_id=book_id, from_user=previous, to_user=to_user_id)


__all__ = [
    "OrphanReport",
    "ReassignResult",
    "UserDetail",
    "UserList",
    "UserRow",
    "inspect_user",
    "list_orphan_books",
    "list_users",
    "reassign_book",
]
