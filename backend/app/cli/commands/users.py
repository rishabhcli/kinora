"""``kinora-admin users`` — user / tenant administration (§5.1)."""

from __future__ import annotations

import argparse

from app.cli.actions import users as actions
from app.cli.context import CliContext
from app.cli.output import Renderable


async def _list(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_users(ctx.container, limit=args.limit)


async def _inspect(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.inspect_user(ctx.container, user_id=args.id, email=args.email)


async def _orphans(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_orphan_books(ctx.container, limit=args.limit)


async def _reassign(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.reassign_book(ctx.container, args.book_id, args.to_user)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``users`` and its subcommands."""
    parser = subparsers.add_parser("users", help="user admin: list / inspect / orphans / reassign")
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_list = sub.add_parser("list", help="list accounts (with owned-book counts)")
    p_list.add_argument("--limit", type=int, default=100, help="max rows (default 100)")
    p_list.set_defaults(func=_list)

    p_inspect = sub.add_parser("inspect", help="inspect one user (by id or email) + their books")
    p_inspect.add_argument("--id", help="user id")
    p_inspect.add_argument("--email", help="user email")
    p_inspect.set_defaults(func=_inspect)

    p_orphans = sub.add_parser("orphans", help="books with no durable owner (user_id IS NULL)")
    p_orphans.add_argument("--limit", type=int, default=200, help="max rows (default 200)")
    p_orphans.set_defaults(func=_orphans)

    p_reassign = sub.add_parser("reassign", help="reassign a book's owner")
    p_reassign.add_argument("book_id")
    p_reassign.add_argument("to_user", help="the target user id")
    p_reassign.set_defaults(func=_reassign)


__all__ = ["add_parser"]
