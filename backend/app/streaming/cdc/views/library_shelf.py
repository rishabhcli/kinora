"""The library-shelf read view — a denormalised projection of ``books``.

The shelf is what the desktop library grid reads: one card per book with the
fields the UI needs (title, author, status, cover key, page count) already
denormalised so the read is a single scan with no joins. It is a textbook
:class:`~app.streaming.cdc.views.view.KeyedProjectionView`: each ``books`` row
projects to one shelf card; a soft-deleted book (``deleted_at`` set) drops off
the shelf; a status change (importing → ready) updates the card in place.

This shows the simplest, highest-value materialised view: a 1:1 projection that
stays current as books are added, imported, and removed — with zero query cost
at read time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.streaming.cdc.views.view import KeyedProjectionView


class LibraryShelfView(KeyedProjectionView):
    """One denormalised shelf card per (non-deleted) book."""

    name = "library_shelf"

    @property
    def source(self) -> str:
        return "books"

    def project(self, row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        # Soft-deleted books leave the shelf entirely.
        if row.get("deleted_at") is not None:
            return None
        return {
            "book_id": row.get("id"),
            "title": row.get("title"),
            "author": row.get("author"),
            "status": row.get("status"),
            "cover_key": row.get("cover_key"),
            "num_pages": row.get("num_pages"),
            "owner_id": row.get("user_id"),
        }


__all__ = ["LibraryShelfView"]
