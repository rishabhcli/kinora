"""Ingest worker — a ``python -m app.ingest.worker`` entrypoint for Phase A.

Self-contained: it builds the real provider bundle + object store and runs
:func:`app.ingest.service.ingest_book` for a given ``book_id``. It is the
deployable form of the §6 "Ingest workers (Phase A)" box.

A later phase's render/scheduler queue (Phase 8) can drive ingest by importing
:func:`run_ingest` directly; if such a queue module is present this worker will
hand off to it, otherwise it falls back to the one-shot CLI below. Run it as:

    python -m app.ingest.worker <book_id>
    KINORA_INGEST_BOOK_ID=<book_id> python -m app.ingest.worker
"""

from __future__ import annotations

import argparse
import importlib
import os

import anyio

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.ingest.service import IngestResult, ingest_book
from app.providers import Providers, create_providers
from app.storage.object_store import ObjectStore

logger = get_logger("app.ingest.worker")

#: Optional Phase-8 queue consumer module; absent until that phase lands.
_QUEUE_MODULE = "app.scheduler.ingest_consumer"


async def _log_progress(stage: str, pct: float) -> None:
    """Default progress sink for the worker (structured log per milestone)."""
    logger.info("ingest.progress", stage=stage, pct=round(pct, 3))


async def run_ingest(
    book_id: str,
    *,
    settings: Settings | None = None,
    providers: Providers | None = None,
) -> IngestResult:
    """Build dependencies and ingest one book; closes owned providers on exit.

    Args:
        book_id: the book to ingest (its PDF is read from ``source_pdf_key``).
        settings: app settings (defaults to the process settings).
        providers: an existing provider bundle to reuse; when ``None`` a fresh one
            is created and closed here.

    Returns:
        The :class:`IngestResult` summary.
    """
    settings = settings or get_settings()
    owns_providers = providers is None
    providers = providers or create_providers(settings)
    store = ObjectStore.from_settings(settings)
    # Make sure the bucket exists (idempotent); never fail ingest on a probe error.
    try:
        await anyio.to_thread.run_sync(store.ensure_bucket)
    except Exception as exc:  # noqa: BLE001 - bucket may be pre-provisioned/read-only
        logger.warning("ingest.worker.ensure_bucket_failed", error=str(exc))
    try:
        return await ingest_book(
            book_id,
            providers=providers,
            blob_store=store,
            settings=settings,
            progress=_log_progress,
        )
    finally:
        if owns_providers:
            await providers.aclose()


def _queue_consumer_available() -> bool:
    """Whether the Phase-8 ingest queue consumer module is importable."""
    try:
        importlib.import_module(_QUEUE_MODULE)
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ingest the book id from argv or ``KINORA_INGEST_BOOK_ID``."""
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest.worker",
        description="Run Kinora Phase A ingest for a single book.",
    )
    parser.add_argument(
        "book_id",
        nargs="?",
        default=os.environ.get("KINORA_INGEST_BOOK_ID"),
        help="book id to ingest (defaults to KINORA_INGEST_BOOK_ID)",
    )
    ns = parser.parse_args(argv)
    if not ns.book_id:
        parser.error("a book_id argument or KINORA_INGEST_BOOK_ID env var is required")

    settings = get_settings()
    configure_logging(settings.log_level)
    if _queue_consumer_available():
        logger.info("ingest.worker.queue_available", module=_QUEUE_MODULE)
    result = anyio.run(run_ingest, ns.book_id)
    logger.info("ingest.worker.done", **result.model_dump())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
