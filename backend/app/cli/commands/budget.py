"""``kinora-admin budget`` — budget administration + reports (§11.1, §13)."""

from __future__ import annotations

import argparse

from app.cli.actions import budget as actions
from app.cli.context import CliContext
from app.cli.output import Renderable


async def _report(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.budget_report(ctx.container, top=args.top)


async def _remaining(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return await actions.budget_remaining(ctx.container)


async def _ledger(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.budget_ledger(
        ctx.container,
        book_id=args.book,
        session_id=args.session,
        scene_id=args.scene,
        limit=args.limit,
    )


async def _caps(ctx: CliContext, _args: argparse.Namespace) -> Renderable:
    return actions.budget_caps(ctx.container)


async def _efficiency(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.budget_efficiency(ctx.container, book_id=args.book)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``budget`` and its subcommands."""
    parser = subparsers.add_parser("budget", help="budget accounting: report / remaining / ledger")
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_report = sub.add_parser("report", help="global accounting + top-spender books")
    p_report.add_argument("--top", type=int, default=10, help="books to show (default 10)")
    p_report.set_defaults(func=_report)

    p_remaining = sub.add_parser("remaining", help="remaining video-seconds + gate state")
    p_remaining.set_defaults(func=_remaining)

    p_ledger = sub.add_parser("ledger", help="the most-recent ledger rows (audit tail)")
    p_ledger.add_argument("--book", help="scope to a book id")
    p_ledger.add_argument("--session", help="scope to a session id")
    p_ledger.add_argument("--scene", help="scope to a scene id")
    p_ledger.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    p_ledger.set_defaults(func=_ledger)

    p_caps = sub.add_parser("caps", help="the configured caps + live-video gate")
    p_caps.set_defaults(func=_caps)

    p_eff = sub.add_parser("efficiency", help="§13 accepted-footage efficiency metric")
    p_eff.add_argument("--book", help="scope to a book id (default: global)")
    p_eff.set_defaults(func=_efficiency)


__all__ = ["add_parser"]
