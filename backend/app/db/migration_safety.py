"""Online-migration patterns and backfill helpers (zero-downtime schema change).

A naive ``ALTER TABLE`` or ``CREATE INDEX`` takes a lock that blocks reads and/or
writes for the duration — fine on an empty dev table, an outage on a populated
production one. This module packages the standard safe patterns so a migration
(or a one-off maintenance script) can apply them without re-deriving the
gotchas each time:

* :func:`set_lock_timeout` / :func:`statement_timeout` — bound how long a DDL
  statement will *wait* for a lock (and run) so a migration fails fast instead of
  queueing behind a long transaction and stalling every query behind it.
* :func:`create_index_concurrently` — emit ``CREATE INDEX CONCURRENTLY`` (no
  table-write lock). Must run **outside** a transaction, so it returns SQL the
  caller runs on an autocommit connection (and the helper guards against being
  called inside a transaction).
* the **expand/contract** column lifecycle (:func:`add_nullable_column`,
  :func:`backfill_column`, :func:`set_not_null_via_check`) — add a column nullable
  (instant), backfill it in batches (no long lock), then enforce ``NOT NULL`` via
  a ``NOT VALID`` check that validates without an ``ACCESS EXCLUSIVE`` lock.
* :class:`BackfillRunner` — run a batched ``UPDATE`` loop keyed by a monotonic
  column, committing each batch so locks are short and replication keeps up.
* :func:`lint_ddl` — a static safety linter that flags risky DDL strings (a bare
  ``CREATE INDEX``, an ``ALTER ... SET NOT NULL`` without a prepared check, etc.)
  for review.

The SQL-emitting helpers return strings (or run them on a given executor) and do
no I/O on import.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.logging import get_logger

logger = get_logger("app.db.migration_safety")

#: A safe default lock-wait ceiling for a migration: fail fast rather than block
#: the whole table behind a slow lock acquisition.
DEFAULT_LOCK_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- #
# Timeout guards
# --------------------------------------------------------------------------- #


def set_lock_timeout_sql(ms: int = DEFAULT_LOCK_TIMEOUT_MS) -> str:
    """SQL to bound how long a statement waits to *acquire* a lock."""
    if ms < 0:
        raise ValueError("lock timeout must be >= 0")
    return f"SET lock_timeout = '{ms}ms'"


def set_statement_timeout_sql(ms: int) -> str:
    """SQL to bound how long a statement may *run* before being cancelled."""
    if ms < 0:
        raise ValueError("statement timeout must be >= 0")
    return f"SET statement_timeout = '{ms}ms'"


async def set_lock_timeout(
    conn: AsyncConnection | AsyncSession, ms: int = DEFAULT_LOCK_TIMEOUT_MS
) -> None:
    """Apply a session ``lock_timeout`` so DDL fails fast on a contended lock."""
    await conn.execute(text(set_lock_timeout_sql(ms)))


async def set_statement_timeout(conn: AsyncConnection | AsyncSession, ms: int) -> None:
    """Apply a session ``statement_timeout``."""
    await conn.execute(text(set_statement_timeout_sql(ms)))


# --------------------------------------------------------------------------- #
# Concurrent index creation
# --------------------------------------------------------------------------- #


def create_index_concurrently_sql(
    *,
    index_name: str,
    table: str,
    columns: list[str],
    unique: bool = False,
    where: str | None = None,
    method: str | None = None,
) -> str:
    """Emit ``CREATE INDEX CONCURRENTLY`` SQL (no table-write lock).

    Must be executed outside a transaction block (Postgres rejects
    ``CONCURRENTLY`` inside one). The ``IF NOT EXISTS`` clause makes a retried
    migration idempotent. ``method`` selects an access method (``gin``, ``hnsw``,
    …); ``where`` builds a partial index.
    """
    if not columns:
        raise ValueError("at least one column is required")
    unique_kw = "UNIQUE " if unique else ""
    using = f" USING {method}" if method else ""
    cols = ", ".join(columns)
    sql = (
        f"CREATE {unique_kw}INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
        f"ON {table}{using} ({cols})"
    )
    if where:
        sql += f" WHERE {where}"
    return sql


def drop_index_concurrently_sql(index_name: str) -> str:
    """Emit ``DROP INDEX CONCURRENTLY IF EXISTS`` SQL (no table lock)."""
    return f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"


async def _assert_not_in_transaction(conn: AsyncConnection) -> None:
    if conn.in_transaction():
        raise RuntimeError(
            "CONCURRENTLY operations must run outside a transaction; "
            "use an autocommit connection (engine.connect().execution_options("
            "isolation_level='AUTOCOMMIT'))"
        )


async def create_index_concurrently(conn: AsyncConnection, **kwargs: Any) -> None:
    """Run :func:`create_index_concurrently_sql` on an autocommit connection."""
    await _assert_not_in_transaction(conn)
    await conn.execute(text(create_index_concurrently_sql(**kwargs)))


# --------------------------------------------------------------------------- #
# Expand / contract column lifecycle
# --------------------------------------------------------------------------- #


def add_nullable_column_sql(table: str, column: str, type_sql: str) -> str:
    """Emit an instant, lock-cheap ``ADD COLUMN ... NULL`` (the expand step).

    Adding a *nullable* column with no default is metadata-only in modern
    Postgres — no table rewrite, no long lock. The default/backfill comes later.
    """
    return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_sql} NULL"


def set_not_null_via_check_sql(table: str, column: str) -> list[str]:
    """Emit the safe ``NOT NULL`` enforcement sequence (the contract step).

    A direct ``SET NOT NULL`` scans the whole table under an ``ACCESS EXCLUSIVE``
    lock. The safe path adds a ``CHECK (col IS NOT NULL) NOT VALID`` (instant),
    ``VALIDATE``s it under a weaker lock, then promotes to a real ``SET NOT NULL``
    (which Postgres can satisfy cheaply once a validated check proves it) and
    drops the scaffolding check. Returns the ordered statements; run each in its
    own short transaction.
    """
    constraint = f"ck_{table}_{column}_not_null_tmp"
    return [
        f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
        f"CHECK ({column} IS NOT NULL) NOT VALID",
        f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}",
        f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL",
        f"ALTER TABLE {table} DROP CONSTRAINT {constraint}",
    ]


def add_foreign_key_not_valid_sql(
    *, table: str, constraint: str, column: str, ref_table: str, ref_column: str = "id"
) -> list[str]:
    """Emit a two-step FK add: ``ADD ... NOT VALID`` then ``VALIDATE``.

    ``ADD CONSTRAINT ... NOT VALID`` takes a brief lock and skips the full-table
    scan; the separate ``VALIDATE CONSTRAINT`` scans under a weaker
    ``SHARE UPDATE EXCLUSIVE`` lock that doesn't block writes.
    """
    return [
        f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
        f"FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) NOT VALID",
        f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}",
    ]


# --------------------------------------------------------------------------- #
# Batched backfill
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BackfillReport:
    """Outcome of a batched backfill run."""

    batches: int = 0
    rows_updated: int = 0
    done: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view."""
        return {"batches": self.batches, "rows_updated": self.rows_updated, "done": self.done}


@dataclass(slots=True)
class BackfillRunner:
    """Run a batched ``UPDATE`` loop with short, per-batch transactions.

    The classic safe backfill: instead of one ``UPDATE`` over millions of rows
    (a long lock + a giant WAL record + replication lag), update a bounded batch
    at a time, committing after each so locks are released and replicas keep up.
    Each batch targets rows by a monotonic ``key_column`` greater than the last
    processed key, so progress is resumable and never re-scans done rows.

    ``update_sql`` is the per-batch statement template; it must accept the bind
    parameters ``:after`` (exclusive lower bound on the key) and ``:limit`` and
    must ``RETURNING`` the key column so the runner can advance the cursor. A
    typical template::

        UPDATE pages SET word_count = char_length(text)
        WHERE id IN (
            SELECT id FROM pages
            WHERE word_count IS NULL AND id > :after
            ORDER BY id LIMIT :limit
        )
        RETURNING id
    """

    update_sql: str
    batch_size: int = 1000
    max_batches: int | None = None
    _last_key: Any = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be > 0")

    async def run(
        self,
        commit: Callable[[], Awaitable[None]],
        execute: Callable[[str, dict[str, Any]], Awaitable[list[Any]]],
        *,
        start_after: Any = "",
    ) -> BackfillReport:
        """Drive the backfill loop.

        ``execute(sql, params)`` runs one batch and returns the list of returned
        key values (the ``RETURNING`` column). ``commit()`` commits the batch.
        Loops until a batch returns no rows (or ``max_batches`` is hit). Injecting
        these two callables keeps the runner testable without a live engine.
        """
        report = BackfillReport()
        self._last_key = start_after
        while True:
            if self.max_batches is not None and report.batches >= self.max_batches:
                break
            keys = await execute(
                self.update_sql, {"after": self._last_key, "limit": self.batch_size}
            )
            await commit()
            report.batches += 1
            report.rows_updated += len(keys)
            if not keys:
                report.done = True
                break
            self._last_key = max(keys)
            logger.info(
                "db.backfill.batch",
                batch=report.batches,
                rows=len(keys),
                last_key=str(self._last_key),
            )
        return report


async def backfill_in_batches(
    session: AsyncSession,
    update_sql: str,
    *,
    batch_size: int = 1000,
    max_batches: int | None = None,
    start_after: Any = "",
) -> BackfillReport:
    """Convenience wrapper: run a :class:`BackfillRunner` against a live session.

    Each batch is committed via ``session.commit()`` so locks stay short. The
    ``update_sql`` template must ``RETURNING`` its monotonic key (see
    :class:`BackfillRunner`).
    """
    runner = BackfillRunner(update_sql=update_sql, batch_size=batch_size, max_batches=max_batches)

    async def _execute(sql: str, params: dict[str, Any]) -> list[Any]:
        result = await session.execute(text(sql), params)
        return list(result.scalars().all())

    async def _commit() -> None:
        await session.commit()

    return await runner.run(_commit, _execute, start_after=start_after)


# --------------------------------------------------------------------------- #
# DDL safety linter
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DdlFinding:
    """One risk a DDL statement triggers, with a suggested safer pattern."""

    severity: str  # "danger" | "warning"
    rule: str
    message: str


def lint_ddl(statement: str) -> list[DdlFinding]:
    """Flag risky DDL patterns in a statement string (static, best-effort).

    Catches the common foot-guns: a blocking ``CREATE INDEX`` (suggest
    ``CONCURRENTLY``), an ``ALTER COLUMN ... SET NOT NULL`` (suggest the
    check-then-validate path), an ``ADD COLUMN ... DEFAULT`` on old Postgres (a
    rewrite), and an unconditional ``UPDATE``/``DELETE`` (suggest batching). It is
    advisory — a reviewer/CI gate, not a parser.
    """
    sql = " ".join(statement.split()).lower()
    findings: list[DdlFinding] = []

    if "create index" in sql and "concurrently" not in sql:
        findings.append(
            DdlFinding(
                severity="danger",
                rule="blocking_create_index",
                message="CREATE INDEX without CONCURRENTLY locks table writes; "
                "use create_index_concurrently_sql().",
            )
        )
    if "set not null" in sql and "not valid" not in sql:
        findings.append(
            DdlFinding(
                severity="danger",
                rule="blocking_set_not_null",
                message="SET NOT NULL scans the table under ACCESS EXCLUSIVE; "
                "use set_not_null_via_check_sql().",
            )
        )
    if "add constraint" in sql and "foreign key" in sql and "not valid" not in sql:
        findings.append(
            DdlFinding(
                severity="warning",
                rule="blocking_add_fk",
                message="ADD FOREIGN KEY validates existing rows under a lock; "
                "use add_foreign_key_not_valid_sql() (NOT VALID + VALIDATE).",
            )
        )
    if "add column" in sql and " default " in sql and "not null" in sql:
        findings.append(
            DdlFinding(
                severity="warning",
                rule="add_column_default_not_null",
                message="ADD COLUMN NOT NULL DEFAULT can rewrite the table on "
                "older Postgres; add nullable, backfill, then enforce.",
            )
        )
    if (sql.startswith("update ") or sql.startswith("delete ")) and " where " not in sql:
        findings.append(
            DdlFinding(
                severity="danger",
                rule="unbounded_dml",
                message="Unbounded UPDATE/DELETE takes one long lock + huge WAL; "
                "batch it with BackfillRunner.",
            )
        )
    return findings


def assert_ddl_safe(statement: str) -> None:
    """Raise when :func:`lint_ddl` finds a ``danger``-severity issue (a CI gate)."""
    dangers = [f for f in lint_ddl(statement) if f.severity == "danger"]
    if dangers:
        joined = "; ".join(f"[{f.rule}] {f.message}" for f in dangers)
        raise ValueError(f"unsafe DDL: {joined}")


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_MS",
    "BackfillReport",
    "BackfillRunner",
    "DdlFinding",
    "add_foreign_key_not_valid_sql",
    "add_nullable_column_sql",
    "assert_ddl_safe",
    "backfill_in_batches",
    "create_index_concurrently",
    "create_index_concurrently_sql",
    "drop_index_concurrently_sql",
    "lint_ddl",
    "set_lock_timeout",
    "set_lock_timeout_sql",
    "set_not_null_via_check_sql",
    "set_statement_timeout",
    "set_statement_timeout_sql",
]
