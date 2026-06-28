"""The seam between an imported item and the §9.1 ingest pipeline.

A connector produces a :class:`~app.integrations.models.SourceItem`; the document
renderer turns it into PDF bytes; *this* protocol is the one step that actually
creates a Kinora book and kicks off Phase-A ingest. It is deliberately tiny and
abstract so the :class:`~app.integrations.service.IntegrationsService` never
imports the container, the object store, or the ingest module directly — the
production implementation lives in :mod:`app.composition` and a fake drives the
service in tests.

The contract: given an owner, a title/author, and rendered PDF bytes, create the
``importing`` book row, persist the PDF, trigger ingest out-of-band, and return
the new ``book_id``. That mirrors exactly what ``POST /books`` does for a manual
upload — imported books are first-class books.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IngestGateway(Protocol):
    """Create a book from rendered PDF bytes and start Phase-A ingest."""

    async def import_pdf(
        self,
        *,
        user_id: str,
        title: str,
        author: str | None,
        pdf_bytes: bytes,
        source: str,
    ) -> str:
        """Create the book, store the PDF, spawn ingest; return the book id.

        Args:
            user_id: the owning reader (durable ``books.user_id``).
            title: the book title (from the normalized document).
            author: the book author, if known.
            pdf_bytes: the rendered ingest-entry PDF.
            source: a provenance tag (the connector name) for logging/art-direction.
        """
        ...


__all__ = ["IngestGateway"]
