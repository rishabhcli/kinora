"""``kinora-admin maint`` — backfills + maintenance jobs (§8, §12)."""

from __future__ import annotations

import argparse

from app.cli.actions import maintenance as actions
from app.cli.context import CliContext
from app.cli.output import Renderable


async def _census(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return await actions.census(ctx.container)


async def _stuck(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.stuck_imports(ctx.container, respawn=args.respawn, limit=args.limit)


async def _cache(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.cache_audit(ctx.container, book_id=args.book)


async def _embedding(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.embedding_coverage(ctx.container, book_id=args.book)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``maint`` and its subcommands."""
    parser = subparsers.add_parser(
        "maint", help="maintenance: census / stuck-imports / cache-audit / embedding-coverage"
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_census = sub.add_parser("census", help="row count per core table")
    p_census.set_defaults(func=_census)

    p_stuck = sub.add_parser("stuck-imports", help="find (and optionally respawn) stuck imports")
    p_stuck.add_argument(
        "--respawn", action="store_true", help="respawn durable recovery for stuck books"
    )
    p_stuck.add_argument("--limit", type=int, default=50, help="max books (default 50)")
    p_stuck.set_defaults(func=_stuck)

    p_cache = sub.add_parser("cache-audit", help="§8.7 shot-cache coverage")
    p_cache.add_argument("--book", help="scope to a book id")
    p_cache.set_defaults(func=_cache)

    p_embedding = sub.add_parser(
        "embedding-coverage", help="§8.2 episodic-store embedding coverage"
    )
    p_embedding.add_argument("--book", help="scope to a book id")
    p_embedding.set_defaults(func=_embedding)


__all__ = ["add_parser"]
