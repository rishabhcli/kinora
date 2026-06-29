"""Run the deterministic simulation sweep from the command line.

    python -m app.verification.simulation                 # default: nominal, 50 seeds
    python -m app.verification.simulation --profile chaos --seeds 200
    python -m app.verification.simulation --strict         # demand reservation resolution
    python -m app.verification.simulation --replay 12345 --profile chaos --archetype erratic

This is the reviewer-facing entry point: it runs the "thousands of seeded fault
schedules" sweep, prints a per-profile summary, and — on the first invariant
violation — shrinks the failing schedule to a minimal reproducer and prints it so
it can be pasted back in via ``--replay``. Exit code is non-zero on any violation,
so it doubles as a CI gate.

Nothing here spends a credit (no provider is ever called; the budget pool is
virtual), so it is safe to run anywhere ``DASHSCOPE_API_KEY`` is set to anything.
"""

from __future__ import annotations

import argparse
import sys

from app.core.logging import configure_logging
from app.verification.simulation.faults import FaultProfile, FaultSchedule
from app.verification.simulation.invariants import CORE_INVARIANTS, STRICT_INVARIANTS
from app.verification.simulation.runner import replay, shrink, sweep
from app.verification.simulation.system import SystemConfig
from app.verification.simulation.workload import ARCHETYPES

_PROFILES = {
    "calm": FaultProfile.calm,
    "nominal": FaultProfile.nominal,
    "chaos": FaultProfile.chaos,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.verification.simulation",
        description="FoundationDB-style deterministic sweep of Kinora's control plane.",
    )
    p.add_argument("--profile", choices=sorted(_PROFILES), default="nominal")
    p.add_argument("--seeds", type=int, default=50, help="number of seeds to sweep")
    p.add_argument(
        "--archetypes",
        nargs="+",
        choices=ARCHETYPES,
        default=list(ARCHETYPES),
        help="reader archetypes to fan each seed across",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="also demand every budget reservation resolves (surfaces the known leak)",
    )
    p.add_argument(
        "--session-ms", type=int, default=90_000, help="simulated reader session length"
    )
    p.add_argument(
        "--no-shrink",
        action="store_true",
        help="do not shrink the first failing schedule",
    )
    p.add_argument(
        "--replay",
        type=int,
        default=None,
        metavar="SEED",
        help="replay a single (seed, profile, archetype) instead of sweeping",
    )
    p.add_argument("--archetype", default="steady", help="archetype for --replay")
    p.add_argument(
        "--log-level", default="WARNING", help="logging level (default WARNING — quiet)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging(args.log_level)

    profile = _PROFILES[args.profile]()
    invariants = STRICT_INVARIANTS if args.strict else CORE_INVARIANTS
    config = SystemConfig(session_duration_ms=args.session_ms)

    # --- single replay path ------------------------------------------------ #
    if args.replay is not None:
        sched = FaultSchedule(seed=args.replay, profile=profile)
        res = replay(sched, archetype=args.archetype, config=config, invariants=invariants)
        print(res.describe())
        return 0 if res.ok else 1

    # --- sweep path -------------------------------------------------------- #
    print(
        f"sweeping {args.seeds} seeds × {len(args.archetypes)} archetypes "
        f"under '{profile.label}' ({'strict' if args.strict else 'core'} invariants)…"
    )
    result = sweep(
        profile=profile,
        seeds=range(args.seeds),
        archetypes=tuple(args.archetypes),
        config=config,
        invariants=invariants,
    )
    print(result.summary())

    if result.ok:
        return 0

    failing = result.first_failure
    assert failing is not None
    print("\nfailing schedule:")
    print(f"  {failing.schedule.describe()}")

    if not args.no_shrink:
        print("\nshrinking to a minimal reproducer…")
        shrunk = shrink(failing, config=config, invariants=invariants)
        print(shrunk.describe())
        print(
            f"\nreplay with:\n  python -m app.verification.simulation "
            f"--replay {shrunk.minimal.seed} --profile {args.profile} "
            f"--archetype {shrunk.archetype} "
            f"{'--strict ' if args.strict else ''}--no-shrink"
        )
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
