"""``kinora-admin canon`` — canon inspection + integrity (§8)."""

from __future__ import annotations

import argparse

from app.cli.actions import canon as actions
from app.cli.context import CliContext
from app.cli.output import Renderable
from app.db.models.enums import EntityType


async def _entities(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    kind = EntityType(args.kind) if args.kind else None
    return await actions.list_entities(
        ctx.container,
        args.book_id,
        beat=args.beat,
        kind=kind,
        entity_key=args.key,
    )


async def _states(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_states(ctx.container, args.book_id)


async def _audit_verify(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.verify_audit_chain(ctx.container, args.book_id)


async def _branches(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.list_branches(ctx.container, args.book_id)


async def _integrity(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.check_integrity(ctx.container, args.book_id)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``canon`` and its subcommands."""
    parser = subparsers.add_parser(
        "canon", help="canon: entities / states / audit-verify / integrity"
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_entities = sub.add_parser(
        "entities", help="active entities at a beat (or one key's versions)"
    )
    p_entities.add_argument("book_id")
    p_entities.add_argument("--beat", type=int, help="resolve as-of this beat (default 0)")
    p_entities.add_argument(
        "--kind", choices=[t.value for t in EntityType], help="filter by entity type"
    )
    p_entities.add_argument("--key", help="show every version of this entity_key")
    p_entities.set_defaults(func=_entities)

    p_states = sub.add_parser("states", help="continuity facts (active + retired)")
    p_states.add_argument("book_id")
    p_states.set_defaults(func=_states)

    p_audit = sub.add_parser("audit-verify", help="verify the canon audit hash chain")
    p_audit.add_argument("book_id")
    p_audit.set_defaults(func=_audit_verify)

    p_branches = sub.add_parser("branches", help="the canon branch registry")
    p_branches.add_argument("book_id")
    p_branches.set_defaults(func=_branches)

    p_integrity = sub.add_parser("integrity", help="structural canon checks (read-only)")
    p_integrity.add_argument("book_id")
    p_integrity.set_defaults(func=_integrity)


__all__ = ["add_parser"]
