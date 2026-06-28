"""``kinora-admin render`` — render-job inspection over the DB mirror (§12.1)."""

from __future__ import annotations

import argparse

from app.cli.actions import render_jobs as actions
from app.cli.context import CliContext
from app.cli.output import Renderable
from app.db.models.enums import RenderJobStatus


async def _jobs(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    status = RenderJobStatus(args.status) if args.status else None
    return await actions.list_jobs(
        ctx.container, status=status, session_id=args.session, limit=args.limit
    )


async def _inspect(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.inspect_job_mirror(ctx.container, args.job_id)


async def _defects(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_defects(ctx.container, args.book_id)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``render`` and its subcommands."""
    parser = subparsers.add_parser(
        "render", help="render jobs: jobs / inspect / defects (DB mirror)"
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_jobs = sub.add_parser("jobs", help="list mirrored render jobs (newest first)")
    p_jobs.add_argument(
        "--status", choices=[s.value for s in RenderJobStatus], help="filter by status"
    )
    p_jobs.add_argument("--session", help="scope to a session id")
    p_jobs.add_argument("--limit", type=int, default=100, help="max rows (default 100)")
    p_jobs.set_defaults(func=_jobs)

    p_inspect = sub.add_parser("inspect", help="one job's mirrored DB row")
    p_inspect.add_argument("job_id")
    p_inspect.set_defaults(func=_inspect)

    p_defects = sub.add_parser("defects", help="a book's logged defects (§9.5/§12.4)")
    p_defects.add_argument("book_id")
    p_defects.set_defaults(func=_defects)


__all__ = ["add_parser"]
