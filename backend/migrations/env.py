"""Alembic migration environment (async).

The database URL is resolved from :class:`app.core.config.Settings` so migrations
use the same configuration as the application. ``target_metadata`` is ``None``
until the data-layer phase, where it becomes::

    from app.db.base import Base
    target_metadata = Base.metadata
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# Importing the models package registers every table on ``Base.metadata`` so
# autogenerate sees the full schema.
from app.db.base import Base
from app.db import models  # noqa: F401  (import for side effect: table registration)

# Alembic Config object, providing access to .ini values.
config = context.config

# Configure Python logging from the .ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime (async) database URL from Settings.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Real model metadata is now the autogenerate target.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emits SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure context against a live connection and run migrations."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through a sync-bridged connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entrypoint for online (connected) migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
