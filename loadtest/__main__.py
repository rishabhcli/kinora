"""``python -m loadtest`` — the Kinora load-test CLI (kinora.md §4/§12).

Drives a named run profile against an **explicitly-provided** target URL, prints
a latency-percentile + throughput + error report, gates it against the profile's
SLOs, and exits non-zero on an SLO violation (so CI can fail a regression).

Usage::

    python -m loadtest --target http://localhost:8000 --profile steady_soak \\
        --users 16 --duration 60 --token "$KINORA_TOKEN" --out report.json

``--dry-run`` resolves and prints the run plan (scenario + workload + expected
arrivals + SLOs) **without issuing any traffic** — safe to run anywhere. A real
run requires ``--target`` and only ever talks to that URL; there is no implicit
default target, by design (the brief's hard rule).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from loadtest import _bootstrap  # noqa: F401 - side effect: puts backend on sys.path

from app.reliability.profiles import ProfileOverrides, get_profile, profile_registry  # noqa: E402
from app.reliability.runner import LoadRunner, RunnerConfig  # noqa: E402
from app.reliability.transport import HttpxTransport  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest",
        description="Kinora generation-on-scroll load tester (reliability toolkit).",
    )
    parser.add_argument(
        "--target",
        help="Base URL of the backend to load (e.g. http://localhost:8000). "
        "Required unless --dry-run.",
    )
    parser.add_argument(
        "--profile",
        default="steady_soak",
        help=f"Run profile. One of: {', '.join(sorted(profile_registry()))}.",
    )
    parser.add_argument("--users", type=int, default=16, help="Closed-model virtual users.")
    parser.add_argument("--duration", type=float, default=60.0, help="Run length (seconds).")
    parser.add_argument(
        "--rps", type=float, default=0.0, help="Open-model target rate (req/s); 0 = profile default."
    )
    parser.add_argument("--book-id", default="book_demo", help="Book id the readers open.")
    parser.add_argument("--token", default=None, help="Bearer access token for auth.")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic run seed.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout (s).")
    parser.add_argument("--out", default=None, help="Write the JSON report to this path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve + print the run plan without issuing any traffic.",
    )
    parser.add_argument(
        "--list-profiles", action="store_true", help="List the available profiles and exit."
    )
    return parser


def _list_profiles() -> int:
    print("Available load profiles:")
    for name, profile in sorted(profile_registry().items()):
        print(f"  {name:<14} {profile.description}")
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    overrides = ProfileOverrides(
        users=args.users, duration_s=args.duration, rate_rps=args.rps, seed=args.seed
    )
    plan = profile.build_workload(overrides)
    doc: dict[str, Any] = {
        "dry_run": True,
        "profile": profile.name,
        "description": profile.description,
        "scenario": profile.scenario_name,
        "workload": plan.describe(),
        "slos": [
            {"name": s.name, "kind": s.kind.value, "target": s.target, "endpoint": s.endpoint}
            for s in profile.slos.slos
        ],
        "target": args.target or "(none — dry run)",
    }
    print(json.dumps(doc, indent=2))
    return 0


async def _execute(args: argparse.Namespace) -> int:
    profile = get_profile(args.profile)
    overrides = ProfileOverrides(
        users=args.users, duration_s=args.duration, rate_rps=args.rps, seed=args.seed
    )
    plan = profile.build_workload(overrides)
    transport = HttpxTransport(args.target, timeout_s=args.timeout, token=args.token)
    runner = LoadRunner(
        transport,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        config=RunnerConfig(book_id=args.book_id, seed=args.seed, token=args.token),
    )
    try:
        report = await runner.run(profile.scenario(), plan)
    finally:
        await transport.aclose()

    report.meta["target"] = args.target
    report.meta["profile"] = profile.name

    print(report.render_text())
    verdict = profile.slos.evaluate_report(report)
    print()
    print(verdict.render_text())

    if args.out:
        payload = {"report": report.to_dict(), "slo": verdict.to_dict()}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote {args.out}")

    return 0 if verdict.passed else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.list_profiles:
        return _list_profiles()
    if args.dry_run:
        return _dry_run(args)
    if not args.target:
        print("error: --target is required for a real run (or use --dry-run).", file=sys.stderr)
        return 2
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())
