"""Repositories for books and their pages."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update

from app.db.base import new_id
from app.db.models.book import Book, Page
from app.db.models.enums import BookStatus
from app.db.repositories.base import BaseRepository


class BookRepo(BaseRepository):
    """Create and query books; transition import status."""

    async def create(
        self,
        *,
        title: str,
        author: str | None = None,
        user_id: str | None = None,
        source_pdf_key: str | None = None,
        status: BookStatus = BookStatus.IMPORTING,
        num_pages: int | None = None,
        art_direction: str | None = None,
        watermark_low_s: float | None = None,
        watermark_high_s: float | None = None,
        commit_horizon_s: float | None = None,
        book_id: str | None = None,
    ) -> Book:
        """Insert a new book row (with its durable owner when known)."""
        book = Book(
            id=book_id or new_id(),
            title=title,
            author=author,
            user_id=user_id,
            source_pdf_key=source_pdf_key,
            status=status,
            num_pages=num_pages,
            art_direction=art_direction,
            watermark_low_s=watermark_low_s,
            watermark_high_s=watermark_high_s,
            commit_horizon_s=commit_horizon_s,
        )
        self.session.add(book)
        await self.session.flush()
        return book

    async def get(self, book_id: str) -> Book | None:
        """Fetch a book by id."""
        return await self.session.get(Book, book_id)

    async def list_all(self) -> list[Book]:
        """Return all books, newest first (the shelf)."""
        stmt = select(Book).order_by(Book.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(self, user_id: str) -> list[Book]:
        """Return the books owned by ``user_id`` (durable shelf), newest first."""
        stmt = (
            select(Book).where(Book.user_id == user_id).order_by(Book.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for_user(self, user_id: str) -> int:
        """Count the books owned by ``user_id`` (the per-user ingest quota gate)."""
        stmt = select(func.count()).select_from(Book).where(Book.user_id == user_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def set_status(self, book_id: str, status: BookStatus) -> None:
        """Transition a book's import status."""
        await self.session.execute(
            update(Book).where(Book.id == book_id).values(status=status)
        )
        await self.session.flush()

    async def set_num_pages(self, book_id: str, num_pages: int) -> None:
        """Record the extracted page count."""
        await self.session.execute(
            update(Book).where(Book.id == book_id).values(num_pages=num_pages)
        )
        await self.session.flush()


class PageRepo(BaseRepository):
    """Create and query extracted pages."""

    async def create(
        self,
        *,
        book_id: str,
        page_number: int,
        image_key: str | None = None,
        text: str | None = None,
        word_boxes: list[dict[str, Any]] | None = None,
        page_id: str | None = None,
    ) -> Page:
        """Insert one page."""
        page = Page(
            id=page_id or new_id(),
            book_id=book_id,
            page_number=page_number,
            image_key=image_key,
            text=text,
            word_boxes=word_boxes,
        )
        self.session.add(page)
        await self.session.flush()
        return page

    async def bulk_insert(self, pages: Iterable[dict[str, Any]]) -> int:
        """Bulk-insert page rows; returns the number inserted."""
        rows = [Page(**page) for page in pages]
        self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def get(self, page_id: str) -> Page | None:
        """Fetch a page by id."""
        return await self.session.get(Page, page_id)

    async def get_by_number(self, book_id: str, page_number: int) -> Page | None:
        """Fetch a page by ``(book_id, page_number)``."""
        stmt = select(Page).where(Page.book_id == book_id, Page.page_number == page_number)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_book(self, book_id: str) -> list[Page]:
        """Return all pages of a book in reading order."""
        stmt = select(Page).where(Page.book_id == book_id).order_by(Page.page_number)
        return list((await self.session.execute(stmt)).scalars().all())
