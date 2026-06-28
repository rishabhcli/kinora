"""Incremental re-ingest — diff a changed source against the persisted pages (§9.1).

When a book's source file changes (a corrected scan, a new edition, an author's
revision), a full re-ingest re-renders + re-analyses + re-plans every page, which
wastes the entire token budget on pages that did not change. This module computes
a **per-page diff** so a re-ingest can touch **only what changed**.

The diff is keyed by **page number + a stable text hash**: a page whose
normalised text hashes identically to the persisted page is *unchanged* and its
analysis/canon contribution can be reused; a page whose text differs is *changed*
and must be re-analysed; pages present only in the new extraction are *added*;
pages only in the old set are *removed*.

This module is a **pure** transform (no DB, no network) over already-extracted
text — the orchestration that *applies* a plan (re-analyse the changed slice, then
re-run shot-plan) lives in the service; keeping the diff pure makes the
classification logic exhaustively unit-testable.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger("app.ingest.diff")

_WS_RE = re.compile(r"\s+")


class PageChange(enum.StrEnum):
    """The diff verdict for one page number."""

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"


def normalize_text(text: str | None) -> str:
    """Collapse whitespace + lowercase so trivial re-flow noise is not a 'change'.

    Two extractions of the same page can differ only in spacing/line-wrapping
    (especially after an EPUB/Story re-layout); normalising before hashing means
    those do not spuriously mark the page changed.
    """
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def text_hash(text: str | None) -> str:
    """Stable content hash of a page's normalised text (sha256 hex, truncated)."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PageDiff:
    """The diff verdict + hashes for one page number."""

    page_number: int
    change: PageChange
    old_hash: str | None
    new_hash: str | None


@dataclass(frozen=True, slots=True)
class IngestDiff:
    """The full per-page diff of a new extraction vs the persisted pages."""

    pages: list[PageDiff] = field(default_factory=list)

    @property
    def changed(self) -> list[int]:
        """Page numbers that changed (re-analyse these)."""
        return [p.page_number for p in self.pages if p.change is PageChange.CHANGED]

    @property
    def added(self) -> list[int]:
        """Page numbers present only in the new extraction (analyse these)."""
        return [p.page_number for p in self.pages if p.change is PageChange.ADDED]

    @property
    def removed(self) -> list[int]:
        """Page numbers present only in the old set (drop these)."""
        return [p.page_number for p in self.pages if p.change is PageChange.REMOVED]

    @property
    def unchanged(self) -> list[int]:
        """Page numbers whose text is identical (reuse their analysis)."""
        return [p.page_number for p in self.pages if p.change is PageChange.UNCHANGED]

    @property
    def to_reanalyze(self) -> list[int]:
        """Pages that need fresh VL analysis (changed ∪ added), sorted."""
        return sorted(self.changed + self.added)

    @property
    def is_identical(self) -> bool:
        """Whether nothing changed (a re-ingest would be a no-op)."""
        return not (self.changed or self.added or self.removed)

    @property
    def num_pages_changed(self) -> int:
        return len(self.changed) + len(self.added) + len(self.removed)


def diff_pages(
    old_texts: Mapping[int, str | None],
    new_texts: Mapping[int, str | None],
) -> IngestDiff:
    """Diff persisted page texts (``old``) against a fresh extraction (``new``).

    Args:
        old_texts: ``{page_number: persisted_text}`` (from the ``pages`` table).
        new_texts: ``{page_number: freshly_extracted_text}``.

    Returns:
        An :class:`IngestDiff` classifying every page number in either set.
    """
    page_numbers = sorted(set(old_texts) | set(new_texts))
    diffs: list[PageDiff] = []
    for page in page_numbers:
        in_old = page in old_texts
        in_new = page in new_texts
        old_h = text_hash(old_texts[page]) if in_old else None
        new_h = text_hash(new_texts[page]) if in_new else None
        if in_old and in_new:
            change = PageChange.UNCHANGED if old_h == new_h else PageChange.CHANGED
        elif in_new:
            change = PageChange.ADDED
        else:
            change = PageChange.REMOVED
        diffs.append(
            PageDiff(page_number=page, change=change, old_hash=old_h, new_hash=new_h)
        )
    result = IngestDiff(pages=diffs)
    logger.info(
        "ingest.diff.computed",
        unchanged=len(result.unchanged),
        changed=len(result.changed),
        added=len(result.added),
        removed=len(result.removed),
    )
    return result


def should_full_reingest(diff: IngestDiff, *, changed_fraction_threshold: float = 0.5) -> bool:
    """Heuristic: is incremental re-ingest still worthwhile, or just redo everything?

    When most of the book changed (≥ ``changed_fraction_threshold`` of pages),
    the bookkeeping overhead of a surgical incremental pass outweighs its saving —
    and a heavily-revised book likely needs a fresh canon anyway — so the caller
    should fall back to a full re-ingest. A small edit stays incremental.
    """
    total = len(diff.pages)
    if total == 0:
        return False
    return (diff.num_pages_changed / total) >= changed_fraction_threshold


__all__ = [
    "IngestDiff",
    "PageChange",
    "PageDiff",
    "diff_pages",
    "normalize_text",
    "should_full_reingest",
    "text_hash",
]
