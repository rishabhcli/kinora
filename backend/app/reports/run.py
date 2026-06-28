"""``python -m app.reports.run`` — generate a report from the command line.

A thin CLI over :class:`~app.reports.service.ReportService` for operators and
CI: build + render a report and either write it to a local file or store it in
object storage + index it. It reuses the same composition root the API uses, so
it runs against the real (or local) database + object store with no extra wiring.

Examples::

    # Render a fleet budget report straight to a local PDF (no DB writes beyond reads):
    python -m app.reports.run --kind budget --format pdf --out budget.pdf

    # Generate + persist + index a reading-progress report for a reader/book:
    python -m app.reports.run --kind reading_progress --book <book_id> \\
        --user <user_id> --format html --store

This spends zero video-seconds and makes no model calls.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.composition import build_container
from app.core.config import get_settings
from app.core.logging import get_logger
from app.reports.db_model import ReportKind
from app.reports.render import ReportFormat, render
from app.reports.service import ReportRequest, ReportService
from app.reports.storage import ReportArtifactStore

logger = get_logger("app.reports.run")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate a Kinora report.")
    p.add_argument("--kind", required=True, choices=[k.value for k in ReportKind])
    p.add_argument("--format", default="pdf", choices=[f.value for f in ReportFormat])
    p.add_argument("--book", dest="book_id", default=None, help="book id (book-scoped reports)")
    p.add_argument("--user", dest="user_id", default=None, help="reader id (reader reports)")
    p.add_argument("--year", type=int, default=None, help="year (year-in-review)")
    p.add_argument("--name", dest="reader_name", default=None, help="reader display name")
    p.add_argument("--out", default=None, help="write the rendered bytes to this local path")
    p.add_argument("--store", action="store_true", help="persist + index the artifact")
    return p.parse_args(argv)


async def run_async(args: argparse.Namespace) -> int:
    container = build_container(get_settings())
    service = ReportService(
        artifact_store=ReportArtifactStore(container.object_store),
        ceiling_seconds=container.settings.budget_ceiling_video_s,
    )
    req = ReportRequest(
        kind=ReportKind(args.kind),
        fmt=ReportFormat(args.format),
        user_id=args.user_id,
        book_id=args.book_id,
        reader_name=args.reader_name,
        year=args.year,
        trigger="cli",
    )
    async with container.session_factory() as session:
        if args.store:
            result = await service.generate(session, req)
            await session.commit()
            data = result.data
            logger.info(
                "stored report %s (%d bytes) at %s; url=%s deduped=%s",
                result.artifact.id,
                len(data),
                result.artifact.storage_key,
                result.download_url,
                result.deduped,
            )
        else:
            report = await service.build_report(session, req)
            data = render(report, ReportFormat(args.format), service.brand_for(req.kind))
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(data)
        logger.info("wrote %d bytes to %s", len(data), args.out)
    elif not args.store:
        sys.stdout.buffer.write(data)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(run_async(args))


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
