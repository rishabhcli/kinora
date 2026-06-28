"""The admin-CLI parser tree + runner (``python -m app.cli`` / ``kinora-admin``).

Builds one argparse tree from the command modules, then runs the selected
async handler inside a :class:`~app.cli.context.CliContext` (which owns the wired
:class:`~app.composition.Container`). Handlers return a
:class:`~app.cli.output.Renderable`; the runner renders it in the chosen format,
prints it, and translates a :class:`~app.cli.errors.CliError` into a clean stderr
message + the carried exit code. Unexpected exceptions propagate (traceback) so
real bugs are visible.

Each handler is ``async (ctx, args) -> Renderable | None``; an optional
``exit_code`` attribute on the result (e.g. the doctor report) overrides the
default success code so health-check scripts get a meaningful status.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from app.cli.commands import books as books_cmd
from app.cli.commands import budget as budget_cmd
from app.cli.commands import canon as canon_cmd
from app.cli.commands import doctor as doctor_cmd
from app.cli.commands import maintenance as maint_cmd
from app.cli.commands import queue as queue_cmd
from app.cli.commands import render as render_cmd
from app.cli.commands import users as users_cmd
from app.cli.context import CliContext, build_context
from app.cli.errors import EXIT_OK, EXIT_USAGE, CliError
from app.cli.output import Format, Renderable, render
from app.core.config import get_settings
from app.core.logging import configure_logging

Handler = Callable[[CliContext, argparse.Namespace], Awaitable[Renderable | None]]

_COMMAND_MODULES = (
    doctor_cmd,
    books_cmd,
    budget_cmd,
    queue_cmd,
    canon_cmd,
    users_cmd,
    render_cmd,
    maint_cmd,
)


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full argparse tree from every command module."""
    parser = argparse.ArgumentParser(
        prog="kinora-admin",
        description="Kinora admin / operations CLI — book, budget, queue, canon, "
        "user, render-job, and maintenance ops over the wired backend.",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=[f.value for f in Format],
        default=Format.TABLE.value,
        help="output format (default: table)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the startup/log lines (only emit the rendered result)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for module in _COMMAND_MODULES:
        module.add_parser(subparsers)
    return parser


async def _run_handler(args: argparse.Namespace, fmt: Format) -> int:
    handler: Handler | None = getattr(args, "func", None)
    if handler is None:  # pragma: no cover - guarded by main()
        return EXIT_USAGE
    async with build_context(fmt) as ctx:
        try:
            result = await handler(ctx, args)
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code
    if result is None:
        return EXIT_OK
    print(render(result, fmt))
    return int(getattr(result, "exit_code", EXIT_OK))


def main(argv: list[str] | None = None) -> int:
    """``kinora-admin`` / ``python -m app.cli`` entrypoint; returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if not getattr(args, "quiet", False):
        settings = get_settings()
        configure_logging(settings.log_level)

    fmt = Format(args.format)
    return asyncio.run(_run_handler(args, fmt))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
