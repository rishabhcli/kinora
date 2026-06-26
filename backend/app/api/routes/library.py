"""Library routes — book covers + (future) richer shelf endpoints (Agent 05, §5.1).

A companion to :mod:`app.api.routes.books` that owns the **cover** surface. The
shelf list itself stays on ``GET /books`` (now carrying a presigned
``cover_url`` per book); this module adds the stable, redirecting image accessor

* ``GET /books/{id}/cover`` — 302-redirect to the presigned cover object
  (``books.cover_key``), authed + ownership-checked, 404 when the book is not the
  caller's or has no cover yet (so a blank/broken cover never leaks — the shelf
  falls back to a generated cover).

It is the home for additional library features (search / genre shelves) as they
land. Registered in :data:`app.api.routes.ROUTERS` after ``books`` so the two
``/books`` routers compose (distinct paths, no overlap).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.db.repositories.book import BookRepo

router = APIRouter(prefix="/books", tags=["library"])


@router.get("/{book_id}/cover")
async def get_book_cover(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> RedirectResponse:
    """Redirect to the presigned cover image for an owned book (§5.1 covers).

    Ownership is the durable ``books.user_id`` column (fail-closed): a book that
    is not the caller's, or one with no ``cover_key`` yet, returns 404 rather than
    a broken redirect.
    """
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)
    if not book.cover_key:
        raise APIError("cover_not_found", "this book has no cover", status=404)
    url = container.object_store.presigned_get_url(book.cover_key)
    return RedirectResponse(url, status_code=302)


__all__ = ["router"]
