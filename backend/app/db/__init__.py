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
  the O(log n) source-span lookup, content-hash cache).
* :mod:`app.db.hashing` — the §8.7 content-hash helper.
"""

from __future__ import annotations
