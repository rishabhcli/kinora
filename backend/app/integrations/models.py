"""The normalized import format every connector produces.

A connector's job is to turn whatever the source looks like (a Readwise JSON
page, a Notion block tree, an RSS entry, a scraped article) into a small,
uniform shape: a :class:`SourceItem` (one importable thing, with a stable id and
a content fingerprint for dedup) carrying a :class:`NormalizedDocument` (an
ordered list of typed text blocks). The document renderer (:mod:`.document`)
turns that into the PDF bytes the §9.1 ingest API already accepts — so the rest
of Kinora never learns a connector exists.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BlockKind(StrEnum):
    """The kind of a normalized text block (drives the rendered styling)."""

    HEADING = "heading"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"
    QUOTE = "quote"
    NOTE = "note"
    DIVIDER = "divider"


class NormalizedBlock(BaseModel):
    """One styled chunk of text in a :class:`NormalizedDocument`."""

    model_config = ConfigDict(frozen=True)

    kind: BlockKind = BlockKind.PARAGRAPH
    text: str = ""
    #: Optional attribution/source for a quote/highlight (e.g. "p. 42", a URL).
    cite: str | None = None

    def word_count(self) -> int:
        """Number of whitespace-delimited words in this block."""
        return len(self.text.split())


class NormalizedDocument(BaseModel):
    """A source item's content, normalized to an ordered list of blocks.

    This is the connector → renderer contract. ``title``/``author`` seed the
    book row; ``blocks`` become the rendered PDF pages the ingest pipeline reads.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    author: str | None = None
    blocks: tuple[NormalizedBlock, ...] = ()
    #: Free-form provenance (source URL, original id, tags) carried for audit;
    #: never required by the renderer.
    metadata: dict[str, str] = Field(default_factory=dict)

    def word_count(self) -> int:
        """Total word count across all blocks (used for empty-doc detection)."""
        return sum(b.word_count() for b in self.blocks)

    def is_empty(self) -> bool:
        """True when the document has no renderable text (skip importing it)."""
        return self.word_count() == 0

    def text(self) -> str:
        """Flatten all blocks to plain text (for hashing / previews)."""
        return "\n\n".join(b.text for b in self.blocks if b.text)


class SourceItem(BaseModel):
    """One importable unit from a source, with a stable id and a content hash.

    ``source_id`` must be stable across syncs (it is the dedup key); ``updated_at``
    drives incremental cursors; ``content_hash`` detects in-place edits so a
    changed item can be re-imported while an unchanged one is skipped.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable id within the source (Readwise book id, Notion page id, article URL).
    source_id: str
    document: NormalizedDocument
    #: When the source last changed this item (for incremental cursors).
    updated_at: datetime | None = None
    #: Opaque per-item tag carried back to the source (URL, raw id) for webhooks.
    external_ref: str | None = None

    @property
    def content_hash(self) -> str:
        """A stable fingerprint of the renderable content (title + author + text).

        Two syncs that produce byte-identical content yield the same hash, so the
        dedup ledger can skip re-importing an unchanged item even when its
        ``updated_at`` is missing or unreliable.
        """
        h = hashlib.sha256()
        h.update(self.document.title.encode("utf-8"))
        h.update(b"\x00")
        h.update((self.document.author or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(self.document.text().encode("utf-8"))
        return h.hexdigest()


class FetchPage(BaseModel):
    """One page of a paginated connector fetch.

    ``next_cursor`` is the connector-defined opaque pagination token; ``None``
    means there are no more pages. ``etag`` lets cheap "nothing changed" 304
    short-circuits ride through the sync engine.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[SourceItem, ...] = ()
    next_cursor: str | None = None
    etag: str | None = None


class SyncCursor(BaseModel):
    """The incremental state the sync engine persists between runs.

    ``high_watermark`` is the largest ``updated_at`` seen so far (the connector
    filters the next fetch to items strictly after it); ``etag`` short-circuits
    an unchanged feed.
    """

    model_config = ConfigDict(frozen=True)

    high_watermark: datetime | None = None
    etag: str | None = None
    #: Connector-specific opaque resume token (provider page cursor across runs).
    opaque: str | None = None


__all__ = [
    "BlockKind",
    "FetchPage",
    "NormalizedBlock",
    "NormalizedDocument",
    "SourceItem",
    "SyncCursor",
]
