"""Per-row scrubbers applied on import (after id-remap + key-rewrite).

Some columns hold values that were derived from the *old* book id and carry a
global uniqueness constraint, so importing them verbatim into a database that
still holds the source book collides:

* ``shots.shot_hash`` — the §8.7 content-address cache key
  ``sha1(book_id + beat_id + canon_version + render_mode + seed + ref_set_hash)``;
  it is ``UNIQUE`` across all shots. Because it embeds the old ``book_id`` it is
  stale after import anyway (the render pipeline recomputes it from the new
  book), so we **re-key it to the new book** by prefixing the new book id. This
  preserves the ``shots.shot_hash`` ↔ ``shot_cache.shot_hash`` association (both
  re-keyed the same way) so the imported cache stays self-consistent, while
  never colliding with the source book's rows.

* ``shot_cache.shot_hash`` — the PK of the §8.7 cache, re-keyed identically.

Re-keying (rather than nulling) keeps round-trip fidelity: a re-export of the
imported book reproduces the same self-consistent cache linkage. The re-key is
deterministic in ``(new_book_id, old_hash)`` so the two tables agree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _rekey_shot_hash(old_hash: str, new_book_id: str) -> str:
    """Namespace a content-address hash under the new book id (collision-free)."""
    return f"{new_book_id}:{old_hash}"


def scrub_shot_row(row: dict[str, Any], *, new_book_id: str) -> None:
    """Re-key ``shots.shot_hash`` to the new book (in place); leave None as-is."""
    value = row.get("shot_hash")
    if isinstance(value, str) and value:
        row["shot_hash"] = _rekey_shot_hash(value, new_book_id)


def scrub_shot_cache_row(row: dict[str, Any], *, new_book_id: str) -> None:
    """Re-key ``shot_cache.shot_hash`` (the PK) to the new book (in place)."""
    value = row.get("shot_hash")
    if isinstance(value, str) and value:
        row["shot_hash"] = _rekey_shot_hash(value, new_book_id)


#: table -> in-place scrubber ``(row, *, new_book_id)``, applied on import.
ROW_SCRUBBERS: dict[str, Callable[..., None]] = {
    "shots": scrub_shot_row,
    "shot_cache": scrub_shot_cache_row,
}


__all__ = ["ROW_SCRUBBERS", "scrub_shot_cache_row", "scrub_shot_row"]
