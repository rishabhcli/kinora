"""``python -m loadtest.canary_cli`` — the synthetic-monitoring canary (kinora.md §13).

Runs the Kinora critical journey (login → library → open → read → seek) once
against an explicitly-provided target, asserts each step's SLA, prints the
per-step verdict, and exits non-zero if the journey failed — so it works both as
a pre-deploy smoke test and as a cron'd availability probe.

Like the load CLI, it requires ``--target`` and only ever talks to that URL.
``--dry-run`` prints the journey definition without issuing traffic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from loadtest import _bootstrap  # noqa: F401 - side effect: puts backend on sys.path

from app.reliability.canary import CanaryRunner, kinora_read_journey  # noqa: E402
from app.reliability.transport import HttpxTransport  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.canary_cli",
        description="Kinora synthetic-monitoring critical-journey canary.",
    )
    parser.add_argument("--target", help="Base URL to probe. Required unless --dry-run.")
    parser.add_argument("--email", default="demo@kinora.local", help="Login email.")
    parser.add_argument(
        "--password",
        default="demo-password-123",  # noqa: S107 - demo creds (AGENTS.md)
        help="Login password.",
    )
    parser.add_argument("--book-id", default="book_demo", help="Book to open.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout (s).")
    parser.add_argument(
        "--intent-sla-ms", type=float, default=250.0, help="Intent step latency SLA (ms)."
    )
    parser.add_argument(
        "--seek-sla-ms", type=float, default=150.0, help="Seek step latency SLA (ms)."
    )
    parser.add_argument("--out", default=None, help="Write the JSON result to this path.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the journey without issuing traffic."
    )
    return parser


def _journey(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    return kinora_read_journey(
        email=args.email,
        password=args.password,
        book_id=args.book_id,
        intent_sla_ms=args.intent_sla_ms,
        seek_sla_ms=args.seek_sla_ms,
    )


def _dry_run(args: argparse.Namespace) -> int:
    journey = _journey(args)
    doc: dict[str, Any] = {
        "dry_run": True,
        "journey": journey.name,
        "steps": [
            {"name": s.name, "sla_ms": s.sla_ms, "expect_status": list(s.expect_status)}
            for s in journey.steps
        ],
        "target": args.target or "(none — dry run)",
    }
    print(json.dumps(doc, indent=2))
    return 0


async def _execute(args: argparse.Namespace) -> int:
    transport = HttpxTransport(args.target, timeout_s=args.timeout)
    runner = CanaryRunner(transport, clock=time.monotonic, stop_on_failure=True)
    try:
        result = await runner.run(_journey(args))
    finally:
        await transport.aclose()

    print(result.render_text())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2)
        print(f"\nWrote {args.out}")
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.dry_run:
        return _dry_run(args)
    if not args.target:
        print("error: --target is required for a real probe (or use --dry-run).", file=sys.stderr)
        return 2
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
