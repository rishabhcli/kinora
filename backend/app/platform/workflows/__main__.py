"""``python -m app.platform.workflows`` — run a durable-workflow worker process.

An opt-in entrypoint that spins a :class:`~app.platform.workflows.worker.Worker`
draining the durable store, mirroring how ``python -m app.jobs`` runs the jobs
framework and ``python -m app.queue.worker`` runs the render queue. It is **not**
wired into the composition root or docker-compose by default (keeping the package
additive and collision-free with the other parallel platform packages); a
deployment would add a ``workflow-worker`` compose service that runs this command
once the store backend is provisioned (see ``DESIGN.md``).

Backend selection:

* with ``KINORA_TEST_DATABASE_URL`` / a real DB URL available, it builds the
  :class:`~app.platform.workflows.db_store.PostgresWorkflowStore`;
* otherwise it falls back to the in-memory store (useful for a local smoke).

The concrete workflows in :mod:`app.platform.workflows.defs` are imported so their
registries are populated; their activities are idempotent simulations in this
build (zero credits, ``KINORA_LIVE_VIDEO`` off).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("app.platform.workflows")


async def _amain() -> None:
    # Importing the defs registers the concrete workflows + activities.
    from app.platform.workflows.defs.episode import EPISODE_ACTIVITIES, EPISODE_WORKFLOWS
    from app.platform.workflows.service import WorkflowService
    from app.platform.workflows.store import WorkflowStore

    store: WorkflowStore
    db_url = os.environ.get("KINORA_DATABASE_URL") or os.environ.get("KINORA_TEST_DATABASE_URL")
    if db_url:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.composition import make_session_factory
        from app.platform.workflows.db_store import PostgresWorkflowStore

        engine = create_async_engine(db_url)
        maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        store = PostgresWorkflowStore(make_session_factory(maker))
        logger.info("workflow worker: using Postgres durable store")
    else:
        from app.platform.workflows.memory_store import InMemoryWorkflowStore

        store = InMemoryWorkflowStore()
        logger.info("workflow worker: using in-memory store (no DB URL configured)")

    service = WorkflowService(store, workflows=EPISODE_WORKFLOWS, activities=EPISODE_ACTIVITIES)
    logger.info(
        "workflow worker starting: %d workflows, %d activities",
        len(EPISODE_WORKFLOWS.names()),
        len(EPISODE_ACTIVITIES.names()),
    )
    await service.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("workflow worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
