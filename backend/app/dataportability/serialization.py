"""ORM-row ↔ portable-dict projection for the exported tables.

The archive stores one JSONL row per database row. Two problems make a naive
``vars(row)`` insufficient, and this module solves both generically (by reading
the SQLAlchemy mapper) so it stays correct as columns are added:

* **Non-JSON column types.** ``DateTime`` columns must serialize to ISO-8601
  strings (and parse back to tz-aware ``datetime``); pgvector ``Vector`` columns
  come back as a list/np-array and must serialize to a plain ``list[float]``.
  ``JSONB`` columns are already JSON-native and pass through.
* **Determinism.** Rows are projected with sorted keys so two exports of the same
  data produce byte-identical archives (important for dedup, backups, and tests).

The projection is *column-complete*: every mapped column is included, so adding a
column to a model automatically flows into the archive without touching this
file (and the migration layer handles the reverse — an old archive missing a new
column imports it as the column default / NULL).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


def _load_vector_type() -> type[Any] | None:
    """Load pgvector's SQLAlchemy type without making it a hard dependency."""
    try:  # pragma: no cover - import guard
        from pgvector.sqlalchemy import Vector
    except Exception:  # pragma: no cover
        return None
    return Vector


_VECTOR_TYPE = _load_vector_type()


def _is_vector_column(column: Any) -> bool:
    if _VECTOR_TYPE is not None and isinstance(column.type, _VECTOR_TYPE):
        return True
    # Fallback: structural sniff on the type's class name.
    return type(column.type).__name__.lower() == "vector"


def _is_datetime_column(column: Any) -> bool:
    return isinstance(column.type, DateTime) or (
        type(column.type).__name__.lower() in {"datetime", "timestamp"}
    )


def _encode_value(value: Any, *, is_vector: bool, is_datetime: bool) -> Any:
    """Encode one column value into a JSON-native form."""
    if value is None:
        return None
    if is_datetime and isinstance(value, _dt.datetime):
        return value.isoformat()
    if is_vector:
        # pgvector returns a numpy array or list; normalize to list[float].
        return [float(x) for x in value]
    return value


def _decode_value(value: Any, *, is_vector: bool, is_datetime: bool) -> Any:
    """Decode one JSON-native value back to its column-native form."""
    if value is None:
        return None
    if is_datetime and isinstance(value, str):
        parsed = _dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.UTC)
        return parsed
    if is_vector and isinstance(value, Sequence):
        return [float(x) for x in value]
    return value


class RowCodec:
    """Project rows of one ORM model to/from portable dicts (column-complete).

    Constructed once per model; caches the per-column encode/decode classification
    from the mapper so projecting a few hundred shots is cheap.
    """

    def __init__(self, model: type[DeclarativeBase]) -> None:
        self.model = model
        mapper = sa_inspect(model)
        self._columns: list[str] = []
        self._vector: set[str] = set()
        self._datetime: set[str] = set()
        for col in mapper.columns:
            name = col.key
            self._columns.append(name)
            if _is_vector_column(col):
                self._vector.add(name)
            elif _is_datetime_column(col):
                self._datetime.add(name)
        self._columns.sort()

    @property
    def columns(self) -> list[str]:
        """The mapped column names (sorted) this codec round-trips."""
        return list(self._columns)

    def to_dict(self, row: DeclarativeBase) -> dict[str, Any]:
        """Project an ORM row to a deterministic, JSON-native dict (sorted keys)."""
        out: dict[str, Any] = {}
        for name in self._columns:
            value = getattr(row, name)
            out[name] = _encode_value(
                value, is_vector=name in self._vector, is_datetime=name in self._datetime
            )
        return out

    def from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Decode a portable dict into kwargs suitable for the model constructor.

        Unknown keys (e.g. a column removed in a newer schema) are dropped;
        missing keys (a column added in a newer schema) are simply absent so the
        column default / NULL applies. This is what makes forward/backward
        schema drift tolerable alongside the explicit migration chain.
        """
        out: dict[str, Any] = {}
        known = set(self._columns)
        for name, value in data.items():
            if name not in known:
                continue
            out[name] = _decode_value(
                value, is_vector=name in self._vector, is_datetime=name in self._datetime
            )
        return out

    def build(self, data: dict[str, Any]) -> DeclarativeBase:
        """Construct a detached ORM instance from a portable dict."""
        return self.model(**self.from_dict(data))


#: Logical table name -> ORM model. The single source of truth for which tables
#: participate in book/canon/account export. Order matters for FK-safe insert
#: (parents before children) on import. ``users`` and ``books`` lead.
def table_registry() -> dict[str, type[DeclarativeBase]]:
    """Return ``{table_name: model}`` for every portable table (import order).

    Built lazily (and re-imported each call) so the module stays import-cheap and
    test isolation can rely on a fresh dict.
    """
    from app.db.models.beat import Beat
    from app.db.models.bitemporal import BitemporalState, CanonAudit, CanonBranch
    from app.db.models.book import Book, Page
    from app.db.models.budget import BudgetLedger
    from app.db.models.continuity import ContinuityState
    from app.db.models.defect import Defect
    from app.db.models.entity import Entity
    from app.db.models.pref import Pref
    from app.db.models.render_job import RenderJob
    from app.db.models.scene import Scene
    from app.db.models.session import Session
    from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
    from app.db.models.user import User

    # Insert order: parents first so FK constraints hold during import.
    return {
        "users": User,
        "books": Book,
        "pages": Page,
        "scenes": Scene,
        "beats": Beat,
        "entities": Entity,
        "continuity_states": ContinuityState,
        "bitemporal_states": BitemporalState,
        "canon_branches": CanonBranch,
        "canon_audit": CanonAudit,
        "shots": Shot,
        "source_span_index": SourceSpanIndex,
        "shot_cache": ShotCache,
        "sessions": Session,
        "render_jobs": RenderJob,
        "budget_ledger": BudgetLedger,
        "defects": Defect,
        "prefs": Pref,
    }


#: The subset of tables that belong to a single book (everything FK'd to a book,
#: directly or transitively). Used by the book-bundle and backup exporters.
BOOK_SCOPED_TABLES: tuple[str, ...] = (
    "books",
    "pages",
    "scenes",
    "beats",
    "entities",
    "continuity_states",
    "bitemporal_states",
    "canon_branches",
    "canon_audit",
    "shots",
    "source_span_index",
    "shot_cache",
    "sessions",
    "render_jobs",
    "budget_ledger",
    "defects",
    "prefs",
)

#: The canon-only subset (the §8 graph) for canon export/import.
CANON_TABLES: tuple[str, ...] = (
    "entities",
    "continuity_states",
    "bitemporal_states",
    "canon_branches",
    "canon_audit",
)


def row_codec_for(table: str) -> RowCodec:
    """Build a :class:`RowCodec` for a logical table name."""
    return RowCodec(table_registry()[table])


__all__ = [
    "BOOK_SCOPED_TABLES",
    "CANON_TABLES",
    "RowCodec",
    "row_codec_for",
    "table_registry",
]
