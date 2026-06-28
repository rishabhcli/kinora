"""The search document model — the unit the index stores and returns.

A :class:`SearchDocument` is a *denormalized projection* of one canon/library
object (a book, page, scene, beat, canon entity, or shot) flattened into the
fields a search engine ranks on:

* ``title`` — the short, high-weight headline field (book title, entity name…);
* ``body`` — the long free-text field (page text, beat summary, shot prompt…);
* ``keywords`` — exact-match keyword fields (status, render mode, aliases…);
* ``facets`` — the keyword fields exposed as drill-down facets;
* ``numbers`` — numeric fields eligible for range filters / sorting;
* ``embedding`` — the optional 1152-d dense vector (the semantic arm).

This is intentionally separate from the canon ORM models (kinora.md §8): the
canon stays the authoritative, versioned source of truth; the search document is
a read-optimised copy keyed by ``(kind, ref_id)`` so a re-index can rebuild it
from the canon at any time without the search layer ever being authoritative.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class DocKind(enum.StrEnum):
    """The kind of canon/library object a search document projects."""

    BOOK = "book"
    PAGE = "page"
    SCENE = "scene"
    BEAT = "beat"
    ENTITY = "entity"
    SHOT = "shot"


#: Per-field boost applied to BM25 sub-scores. Title matches matter more than
#: body matches; keyword matches sit between. Tunable without touching ranking.
FIELD_BOOSTS: dict[str, float] = {
    "title": 3.0,
    "name": 3.0,
    "keywords": 2.0,
    "body": 1.0,
}


@dataclass
class SearchDocument:
    """One indexable document: a flattened projection of a canon/library object.

    ``doc_id`` is the index-wide unique id, conventionally ``{kind}:{ref_id}`` so
    a re-project of the same source object overwrites its prior document (upsert
    semantics, not a duplicate). ``book_id`` scopes a search to one work; ``None``
    is allowed for library-wide docs.
    """

    doc_id: str
    kind: DocKind
    ref_id: str
    book_id: str | None = None
    title: str = ""
    body: str = ""
    keywords: list[str] = field(default_factory=list)
    facets: dict[str, str] = field(default_factory=dict)
    numbers: dict[str, float] = field(default_factory=dict)
    embedding: list[float] | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def text_fields(self) -> dict[str, str]:
        """The analyzable text fields, by name (title/name → ``title``, body, keywords)."""
        return {
            "title": self.title,
            "body": self.body,
            "keywords": " ".join(self.keywords),
        }

    def all_text(self) -> str:
        """Every text field concatenated (for the dense-arm document embedding)."""
        return " ".join(p for p in (self.title, self.body, " ".join(self.keywords)) if p)

    def facet_value(self, name: str) -> str | None:
        """Resolve a facet/keyword field value (kind is an implicit facet)."""
        if name == "kind":
            return self.kind.value
        if name == "book_id":
            return self.book_id
        return self.facets.get(name)

    def number(self, name: str) -> float | None:
        """Resolve a numeric field value for range filtering / sorting."""
        return self.numbers.get(name)


def make_doc_id(kind: DocKind, ref_id: str) -> str:
    """The canonical index id for a projected object (``{kind}:{ref_id}``)."""
    return f"{kind.value}:{ref_id}"


__all__ = ["FIELD_BOOSTS", "DocKind", "SearchDocument", "make_doc_id"]
