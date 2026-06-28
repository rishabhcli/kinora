"""``kinora-admin queue`` — render-queue inspection + DLQ operations (§12.1)."""

from __future__ import annotations

import argparse

from app.cli.actions import queue as actions
from app.cli.context import CliContext
from app.cli.output import Renderable
from app.db.models.enums import RenderPriority


async def _stats(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return await actions.queue_stats(ctx.container.queue)


async def _inspect(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.inspect_job(ctx.container.queue, args.job_id)


async def _dlq(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_dlq(ctx.container.queue, limit=args.limit)


async def _replay(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.replay_job(ctx.container.queue, args.job_id)


async def _purge_dlq(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return await actions.purge_dlq(ctx.container.queue)


async def _reap(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return await actions.reap_expired(ctx.container.queue)


async def _cancel(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    lanes = [RenderPriority(p) for p in args.lane] if args.lane else None
    return await actions.cancel_token(ctx.container.queue, args.token, lanes=lanes)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``queue`` and its subcommands."""
    parser = subparsers.add_parser(
        "queue", help="render queue: stats / dlq / replay / reap / cancel"
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_stats = sub.add_parser("stats", help="lane depths + lifetime counters")
    p_stats.set_defaults(func=_stats)

    p_inspect = sub.add_parser("inspect", help="one job's full Redis record")
    p_inspect.add_argument("job_id")
    p_inspect.set_defaults(func=_inspect)

    p_dlq = sub.add_parser("dlq", help="list dead-lettered jobs")
    p_dlq.add_argument("--limit", type=int, default=100, help="max rows (default 100)")
    p_dlq.set_defaults(func=_dlq)

    p_replay = sub.add_parser("replay", help="re-enqueue a dead-lettered job")
    p_replay.add_argument("job_id")
    p_replay.set_defaults(func=_replay)

    p_purge = sub.add_parser("purge-dlq", help="clear the dead-letter list")
    p_purge.set_defaults(func=_purge_dlq)

    p_reap = sub.add_parser("reap", help="re-queue jobs with expired worker leases")
    p_reap.set_defaults(func=_reap)

    p_cancel = sub.add_parser("cancel", help="flag every job on a cancel token")
    p_cancel.add_argument("token")
    p_cancel.add_argument(
        "--lane",
        action="append",
        choices=[p.value for p in RenderPriority],
        help="restrict to lane(s) (repeatable); default: all lanes",
    )
    p_cancel.set_defaults(func=_cancel)


__all__ = ["add_parser"]
