"""Collision-free id remapping on import — preserve every intra-archive reference.

A book bundle is a graph of rows whose primary keys (and foreign keys) refer to
each other. Importing it into a *different* database — possibly one that already
holds the original book — must reassign every primary key to a fresh id while
**rewriting every reference in lockstep**, so the imported graph is internally
consistent and never collides with existing rows.

This module is the reference map. It is deliberately *declarative*: the set of
columns that hold an id reference is described by :data:`REFERENCE_COLUMNS`, and
the remapper rewrites exactly those, leaving everything else untouched. A
reference whose target id is not present in the archive is a
:class:`ReferentialIntegrityError` (import fails closed).

Three kinds of reference exist, handled distinctly:

* **primary key** (``id``) — minted fresh, recorded so references resolve to it;
* **scalar reference** — a column holding one id (``book_id``, ``scene_id``,
  ``shot_id``, ``supersedes``, ``session_id``, …);
* **embedded reference** — an id buried inside a JSON value
  (``shots.reference_image_ids`` carries ``"<entity_key>@v3"``, where the
  entity_key is book-stable so only the book prefix matters — see below).

Note on ``entity_key`` / ``scene_id`` / ``beat_id`` *strings*: the canon
identity strings (``char_elsa``, ``scene_005``, ``beat_0034``) are stable *within
a book* and are not globally unique, so they are **not** remapped — they remain
valid because the whole sub-graph that uses them is re-homed under one new
``book_id``. Only true row ids (UUID-like PKs / FKs) are remapped.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.dataportability.errors import ReferentialIntegrityError
from app.db.base import new_id


class IdSpace:
    """A named space of ids being remapped (e.g. ``"book"``, ``"shot"``).

    Separating spaces means a shot id and a scene id that happen to be equal
    strings never alias each other. In practice ids are UUID4 hex and collide
    with probability ~0, but the spaces keep the model exact and the tests honest.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._map: dict[str, str] = {}

    def mint(self, old_id: str, *, forced: str | None = None) -> str:
        """Assign a fresh id for ``old_id`` (idempotent within this space)."""
        if old_id in self._map:
            return self._map[old_id]
        new = forced if forced is not None else new_id()
        self._map[old_id] = new
        return new

    def resolve(self, old_id: str) -> str | None:
        """Return the new id for ``old_id`` if it has been minted, else ``None``."""
        return self._map.get(old_id)

    def __contains__(self, old_id: str) -> bool:
        return old_id in self._map

    @property
    def mapping(self) -> dict[str, str]:
        """The full old→new mapping (read-only view; copy)."""
        return dict(self._map)


#: For each table, which scalar columns hold an id reference into another table.
#: ``"id"`` is the primary key (minted into the table's own space). The value is
#: the *id space name* the reference points into. Spaces are usually the table
#: name; ``book``/``user`` are shared singletons.
#:
#: This mirrors the FK graph in ``app/db/models`` (see each model's docstring).
REFERENCE_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"id": "user"},
    "books": {"id": "book", "user_id": "user"},
    "pages": {"id": "page", "book_id": "book"},
    "scenes": {"id": "scene_row", "book_id": "book"},
    "beats": {"id": "beat_row", "book_id": "book", "scene_id": "scene_row"},
    "entities": {"id": "entity", "book_id": "book", "supersedes": "entity"},
    "continuity_states": {"id": "continuity", "book_id": "book"},
    "bitemporal_states": {"id": "bitemporal", "book_id": "book"},
    "canon_branches": {"id": "branch_row", "book_id": "book"},
    "canon_audit": {"id": "audit", "book_id": "book"},
    "shots": {"id": "shot", "book_id": "book"},
    "source_span_index": {"id": "span", "book_id": "book", "shot_id": "shot"},
    "shot_cache": {"book_id": "book"},  # PK is shot_hash (content-addressed; kept)
    "sessions": {"id": "session", "user_id": "user", "book_id": "book"},
    "render_jobs": {
        "id": "render_job",
        "session_id": "session",
        "shot_id": "shot",
    },
    "budget_ledger": {
        "id": "budget",
        "book_id": "book",
        "session_id": "session",
    },
    "defects": {"id": "defect", "shot_id": "shot", "book_id": "book"},
    "prefs": {"id": "pref", "user_id": "user", "book_id": "book"},
}

#: Columns that are a primary key (minted) vs a reference (resolved). A column is
#: a PK iff its name is ``"id"``; ``shot_cache`` has none (its PK is the
#: content hash, intentionally preserved across import so the cache still hits).
_PK_COLUMN = "id"


class IdRemapper:
    """Two-phase id remapper: mint all PKs, then rewrite all references.

    Phase 1 (:meth:`mint_table`) walks every row of every table and mints a fresh
    id for each primary key, recording old→new in the right space. Phase 2
    (:meth:`rewrite_row`) rewrites a row's PK and all of its scalar/embedded
    references using the now-complete mapping, failing closed on a dangling ref.

    Two phases are required because references can point forward (a shot's
    ``scene_id`` to a scene row that appears later in the archive, or
    ``entities.supersedes`` to another version of the same chain).
    """

    def __init__(self) -> None:
        self._spaces: dict[str, IdSpace] = {}

    def space(self, name: str) -> IdSpace:
        """Get (or create) the named id space."""
        if name not in self._spaces:
            self._spaces[name] = IdSpace(name)
        return self._spaces[name]

    def force_book_id(self, old_book_id: str, new_book_id: str) -> str:
        """Pin a specific new book id (e.g. when importing into a pre-created row)."""
        return self.space("book").mint(old_book_id, forced=new_book_id)

    def force_user_id(self, old_user_id: str, new_user_id: str) -> str:
        """Pin the importing user's id so owned rows attach to the caller."""
        return self.space("user").mint(old_user_id, forced=new_user_id)

    # -- phase 1: mint every primary key ------------------------------------- #

    def mint_table(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        """Mint fresh ids for every PK in ``rows`` of ``table`` (phase 1)."""
        spec = REFERENCE_COLUMNS.get(table, {})
        pk_space = spec.get(_PK_COLUMN)
        if pk_space is None:
            return  # no remappable PK (e.g. shot_cache keeps its content hash)
        space = self.space(pk_space)
        for row in rows:
            old = row.get(_PK_COLUMN)
            if isinstance(old, str):
                space.mint(old)

    # -- phase 2: rewrite a row's PK + references ---------------------------- #

    def rewrite_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``row`` with its PK + every reference remapped."""
        spec = REFERENCE_COLUMNS.get(table, {})
        out = dict(row)
        for column, space_name in spec.items():
            value = out.get(column)
            if value is None:
                continue
            if not isinstance(value, str):
                continue
            space = self.space(space_name)
            resolved = space.resolve(value)
            if resolved is None:
                # PK columns were all minted in phase 1, so an unresolved PK is a
                # bug; an unresolved *reference* is a dangling pointer in the
                # archive — fail closed either way.
                raise ReferentialIntegrityError(table, column, value)
            out[column] = resolved
        self._rewrite_embedded(table, out)
        return out

    def _rewrite_embedded(self, table: str, row: dict[str, Any]) -> None:
        """Rewrite id references buried inside JSON columns (in place).

        Only ``budget_ledger.reservation_id`` is a true id reference embedded in a
        non-FK column (a reserve row points at itself; commit/release rows point
        at the reserve row), so it must follow the same remap as ``id``.
        ``reference_image_ids`` / ``entity_key`` carry book-stable canon strings
        that are intentionally preserved (see the module docstring).
        """
        if table == "budget_ledger":
            res = row.get("reservation_id")
            if isinstance(res, str):
                # The reservation id is in the "budget" space (it is a budget row id).
                resolved = self.space("budget").resolve(res)
                if resolved is None:
                    raise ReferentialIntegrityError(table, "reservation_id", res)
                row["reservation_id"] = resolved

    # -- introspection ------------------------------------------------------- #

    def mapping(self) -> dict[str, dict[str, str]]:
        """The full set of old→new mappings, keyed by space (for debugging/tests)."""
        return {name: space.mapping for name, space in self._spaces.items()}


__all__ = ["REFERENCE_COLUMNS", "IdRemapper", "IdSpace"]
