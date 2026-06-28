"""Kinora data layer: SQLAlchemy models, async engine/session, repositories.

The package is organised as:

* :mod:`app.db.base` — the declarative :class:`~app.db.base.Base`, a stable
  Alembic naming convention, and shared column mixins.
* :mod:`app.db.session` — the async engine, session factory, and the
  :func:`~app.db.session.get_session` context manager / dependency.
* :mod:`app.db.models` — one module per aggregate; importing the package
  registers every table on ``Base.metadata`` (used by Alembic autogenerate).
* :mod:`app.db.repositories` — async, typed repositories holding the real
  queries (versioning, beat-interval forgetting, pgvector episodic search,
  the O(log n) source-span lookup, content-hash cache), plus the generic
  :class:`~app.db.repositories.generic.GenericRepository` base concrete repos can
  adopt.
* :mod:`app.db.hashing` — the §8.7 content-hash helper.

Shared data-access **infrastructure** (the floor §8/§12 stand on) lives in:

* :mod:`app.db.engine` — typed :class:`~app.db.engine.EngineConfig`, the primary
  + optional read-replica :class:`~app.db.engine.EngineRegistry`, pool tuning,
  and the slow-query recorder/listeners.
* :mod:`app.db.routing` — the read/write split
  (:class:`~app.db.routing.RoutingSessionFactory`): reads to the replica, writes
  to the primary, with a safe single-node fallback.
* :mod:`app.db.health` — connection ``ping`` + pool-stats snapshots for the
  ``/ready`` gate and the §12.5 observability panel.
* :mod:`app.db.mixins` — opt-in soft-delete / audit / optimistic-version column
  mixins.
* :mod:`app.db.unit_of_work` — the :class:`~app.db.unit_of_work.UnitOfWork`
  transaction boundary with savepoints + a repository registry, and
  :func:`~app.db.unit_of_work.run_in_uow` (retry-on-serialization-failure).
* :mod:`app.db.retry` — transient-error classification + bounded exponential
  backoff.
* :mod:`app.db.query` — pagination (offset + keyset), filter/order DSL,
  IN-chunking.
* :mod:`app.db.inspect` — ``EXPLAIN`` plan analysis, ``pg_stat_statements``
  top-N, and the in-process slow-query feed.
* :mod:`app.db.bulk` — chunked bulk insert / ``ON CONFLICT`` upsert.
* :mod:`app.db.migration_safety` — online-DDL patterns + a batched backfill
  runner + a DDL safety linter.
"""

from __future__ import annotations
