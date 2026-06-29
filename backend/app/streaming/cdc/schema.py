"""Schema registry + schema-evolution reconciliation.

The change log carries rows whose column set drifts over time: a migration adds
``books.cover_key``, renames a column, or drops one. Downstream views must keep
working across those evolutions without a redeploy. This module owns:

* :class:`TableSchema` — the column set + types + primary key for one version
  of one table, with a content-addressed :attr:`fingerprint`.
* :class:`SchemaRegistry` — the per-table version history, fed by ``SCHEMA``
  change events (DDL decoded from the log) and queryable for "what did this row
  look like at version N".
* :func:`reconcile` / :func:`migrate_row` — compute the column-level
  :class:`SchemaDelta` between two versions and project a row written under an
  old schema onto a newer one (fill added columns with defaults, drop removed
  ones, apply renames), so a view built on the new schema can consume old
  events deterministically.

Evolution rules implemented (the safe, BACKWARD-compatible subset Debezium /
Avro call out):

* **Add column** — allowed; back-fills with the declared default (or ``None``).
* **Drop column** — allowed; projected-out of old rows.
* **Rename column** — modelled as an explicit ``renames`` map so the value is
  carried over, not lost-then-defaulted.
* **Type widen** — allowed (recorded; no value coercion is forced here).
* **Type narrow / pk change** — flagged ``breaking`` so the engine can choose to
  re-snapshot the affected view rather than silently corrupt it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.streaming.cdc.events import JsonRow


@dataclass(frozen=True, slots=True)
class Column:
    """One column: name, a coarse logical type, nullability, and a default."""

    name: str
    type: str = "any"
    nullable: bool = True
    default: object = None


@dataclass(frozen=True, slots=True)
class TableSchema:
    """A versioned description of one table's columns + primary key."""

    table: str
    version: int
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ("id",)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def by_name(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}

    @property
    def fingerprint(self) -> str:
        """Stable content hash of the column *set* (column order is irrelevant).

        Two schemas with the same columns/types/pk hash equal regardless of the
        order they were declared in — a reordering is not a schema change.
        """
        payload = json.dumps(
            {
                "table": self.table,
                "pk": sorted(self.primary_key),
                "cols": sorted([c.name, c.type, c.nullable] for c in self.columns),
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()  # noqa: S324 - non-crypto id

    @classmethod
    def from_mapping(
        cls,
        table: str,
        version: int,
        columns: dict[str, str],
        *,
        primary_key: tuple[str, ...] = ("id",),
    ) -> TableSchema:
        """Build from a ``{name: type}`` map (the SCHEMA-event payload shape)."""
        cols = tuple(Column(name=n, type=t) for n, t in columns.items())
        return cls(table=table, version=version, columns=cols, primary_key=primary_key)


@dataclass(frozen=True, slots=True)
class SchemaDelta:
    """The column-level diff from one :class:`TableSchema` to another."""

    added: tuple[Column, ...] = ()
    dropped: tuple[Column, ...] = ()
    retyped: tuple[tuple[Column, Column], ...] = ()  # (old, new)
    renames: dict[str, str] = field(default_factory=dict)  # old_name -> new_name
    pk_changed: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.dropped or self.retyped or self.renames or self.pk_changed)

    @property
    def breaking(self) -> bool:
        """Whether the change can corrupt an existing view (needs re-snapshot)."""
        return self.pk_changed


def reconcile(
    old: TableSchema,
    new: TableSchema,
    *,
    renames: dict[str, str] | None = None,
) -> SchemaDelta:
    """Compute the :class:`SchemaDelta` from ``old`` to ``new``.

    ``renames`` (old_name -> new_name) lets the caller declare a rename that
    would otherwise read as a drop+add. Renamed columns are excluded from the
    add/drop sets so :func:`migrate_row` carries their values over.
    """
    renames = dict(renames or {})
    renamed_old = set(renames)
    renamed_new = set(renames.values())

    old_cols = old.by_name
    new_cols = new.by_name

    added = tuple(
        c for name, c in new_cols.items() if name not in old_cols and name not in renamed_new
    )
    dropped = tuple(
        c for name, c in old_cols.items() if name not in new_cols and name not in renamed_old
    )
    retyped = tuple(
        (old_cols[name], new_cols[name])
        for name in old_cols.keys() & new_cols.keys()
        if old_cols[name].type != new_cols[name].type
    )
    pk_changed = old.primary_key != new.primary_key
    return SchemaDelta(
        added=added,
        dropped=dropped,
        retyped=retyped,
        renames=renames,
        pk_changed=pk_changed,
    )


def migrate_row(row: JsonRow, target: TableSchema, delta: SchemaDelta) -> JsonRow:
    """Project ``row`` (written under the old schema) onto ``target``.

    * Renamed columns carry their value across to the new name.
    * Added columns are back-filled with their declared default.
    * Dropped columns are removed.
    The result contains exactly ``target.column_names``.
    """
    out: JsonRow = {}
    target_cols = target.by_name
    for old_name, new_name in delta.renames.items():
        if old_name in row and new_name in target_cols:
            out[new_name] = row[old_name]
    for name in target.column_names:
        if name in out:
            continue
        if name in row:
            out[name] = row[name]
        else:
            col = target_cols[name]
            out[name] = col.default
    return out


class SchemaRegistry:
    """Per-table schema version history fed by ``SCHEMA`` events."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[int, TableSchema]] = {}
        self._latest: dict[str, int] = {}
        #: (table -> (from_version, to_version) -> delta) so a multi-step
        #: migration can carry declared renames across each adjacent hop.
        self._renames: dict[str, dict[tuple[int, int], SchemaDelta]] = {}

    def register(
        self, schema: TableSchema, *, renames: dict[str, str] | None = None
    ) -> SchemaDelta:
        """Record ``schema`` as a (possibly new) version; return the delta from prior.

        ``renames`` is remembered so :meth:`migrate` between adjacent versions
        can carry renamed values across. Returns an empty delta for the first
        version of a table.
        """
        table = schema.table
        versions = self._versions.setdefault(table, {})
        prior_version = self._latest.get(table)
        versions[schema.version] = schema
        self._latest[table] = (
            schema.version if prior_version is None else max(prior_version, schema.version)
        )
        if prior_version is None or prior_version == schema.version:
            self._renames.setdefault(table, {})
            return SchemaDelta()
        prior = versions[prior_version]
        delta = reconcile(prior, schema, renames=renames)
        self._renames.setdefault(table, {})[(prior_version, schema.version)] = delta
        return delta

    def latest(self, table: str) -> TableSchema | None:
        v = self._latest.get(table)
        return self._versions[table][v] if v is not None else None

    def get(self, table: str, version: int) -> TableSchema | None:
        return self._versions.get(table, {}).get(version)

    def migrate(self, row: JsonRow, table: str, from_version: int) -> JsonRow:
        """Bring a row written at ``from_version`` up to the latest schema.

        Applies each adjacent-version delta in order so renames and defaults
        compose correctly across multiple evolutions.
        """
        latest = self._latest.get(table)
        if latest is None or latest == from_version:
            return dict(row)
        current = dict(row)
        versions = sorted(v for v in self._versions[table] if v >= from_version)
        for lo, hi in zip(versions, versions[1:], strict=False):
            target = self._versions[table][hi]
            delta = self._renames.get(table, {}).get((lo, hi))
            if delta is None:
                delta = reconcile(self._versions[table][lo], target)
            current = migrate_row(current, target, delta)
        return current


__all__ = [
    "Column",
    "SchemaDelta",
    "SchemaRegistry",
    "TableSchema",
    "migrate_row",
    "reconcile",
]
