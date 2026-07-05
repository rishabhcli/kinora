"""``kinora-admin books`` — book-lifecycle operations."""

from __future__ import annotations

import argparse

from app.cli.actions import books as actions
from app.cli.actions import review_export
from app.cli.context import CliContext
from app.cli.output import Renderable
from app.db.models.enums import BookStatus


async def _list(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    status = BookStatus(args.status) if args.status else None
    return await actions.list_books(ctx.container, status=status, limit=args.limit)


async def _inspect(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.inspect_book(ctx.container, args.book_id)


async def _set_status(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.set_book_status(ctx.container, args.book_id, BookStatus(args.status))


async def _reingest(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.reingest_book(
        ctx.container, args.book_id, reset_status=not args.no_reset, force=args.force
    )


async def _delete(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await actions.delete_book(
        ctx.container, args.book_id, purge_storage=not args.keep_storage
    )


async def _export_review(ctx: CliContext, args: argparse.Namespace) -> Renderable:
    return await review_export.export_book_review(
        ctx.container, args.book_id, args.out, max_shots=args.max_shots
    )


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``books`` and its subcommands."""
    parser = subparsers.add_parser(
        "books", help="book lifecycle: list / inspect / reingest / delete"
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)

    p_list = sub.add_parser("list", help="list the shelf (newest first)")
    p_list.add_argument(
        "--status", choices=[s.value for s in BookStatus], help="filter by import status"
    )
    p_list.add_argument("--limit", type=int, default=100, help="max rows (default 100)")
    p_list.set_defaults(func=_list)

    p_inspect = sub.add_parser("inspect", help="deep-inspect one book (counts + budget)")
    p_inspect.add_argument("book_id")
    p_inspect.set_defaults(func=_inspect)

    p_status = sub.add_parser("set-status", help="manually transition a book's import status")
    p_status.add_argument("book_id")
    p_status.add_argument("status", choices=[s.value for s in BookStatus])
    p_status.set_defaults(func=_set_status)

    p_reingest = sub.add_parser("reingest", help="re-run Phase-A ingest from the source PDF")
    p_reingest.add_argument("book_id")
    p_reingest.add_argument(
        "--no-reset", action="store_true", help="do not flip status back to importing first"
    )
    p_reingest.add_argument(
        "--force",
        action="store_true",
        help=(
            "force-clear an active ingest lock (no heartbeat exists to tell a stale lock "
            "apart from a genuinely in-progress ingest — only pass this once you've "
            "confirmed the original process is dead, e.g. the api container restarted)"
        ),
    )
    p_reingest.set_defaults(func=_reingest)

    p_delete = sub.add_parser("delete", help="hard-delete a book (cascades) + purge its blobs")
    p_delete.add_argument("book_id")
    p_delete.add_argument("--keep-storage", action="store_true", help="do not purge object storage")
    p_delete.set_defaults(func=_delete)

    p_export = sub.add_parser(
        "export-review",
        help="export a reading-order script + downloaded clips + a local HTML viewer",
    )
    p_export.add_argument("book_id")
    p_export.add_argument("--out", required=True, help="local directory to write the review into")
    p_export.add_argument(
        "--max-shots", type=int, default=None, help="cap on shots exported (default: all)"
    )
    p_export.set_defaults(func=_export_review)


__all__ = ["add_parser"]
