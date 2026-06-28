"""``kinora-admin doctor`` — one-shot health probe across every dependency."""

from __future__ import annotations

import argparse

from app.cli.actions.doctor import DoctorReport, run_doctor
from app.cli.context import CliContext


async def _handle(ctx: CliContext, _args: argparse.Namespace) -> DoctorReport:
    return await run_doctor(ctx.container)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``doctor``."""
    parser = subparsers.add_parser(
        "doctor",
        help="probe Postgres / Redis / object store / queue + budget gate",
        description="Health-check every dependency; exits non-zero if any probe fails.",
    )
    parser.set_defaults(func=_handle)


__all__ = ["add_parser"]
