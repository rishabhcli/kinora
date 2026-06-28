"""``python -m loadtest.capacity_cli`` — offline capacity planning (kinora.md §4/§11).

A pure, **offline** planner (no target, no traffic): given a reader population,
a per-shot render latency, and the video budget, it prints the render demand, an
M/M/c worker-sizing estimate, the §4.5 watermark feasibility, and the §11 budget
runway. This is the "how many workers / how much budget" answer for a deployment,
derived from the same capacity model the unit tests pin.
"""

from __future__ import annotations

import argparse
import json

from loadtest import _bootstrap  # noqa: F401 - side effect: puts backend on sys.path

from app.reliability.capacity import (  # noqa: E402
    BudgetRunway,
    ReadingProfile,
    RenderDemand,
    max_concurrent_readers,
    min_servers_for_utilisation,
    mmc_queue,
    watermark_feasibility,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.capacity_cli",
        description="Offline capacity planner: readers -> workers -> budget (no traffic).",
    )
    parser.add_argument("--readers", type=int, default=50, help="Concurrent readers.")
    parser.add_argument("--velocity-wps", type=float, default=4.0, help="Reading velocity (wps).")
    parser.add_argument(
        "--active-fraction", type=float, default=0.7, help="Fraction of time actively reading."
    )
    parser.add_argument(
        "--render-latency-s", type=float, default=60.0, help="Mean per-shot render wall-clock (s)."
    )
    parser.add_argument(
        "--seconds-per-shot", type=float, default=5.0, help="Video-seconds per shot."
    )
    parser.add_argument(
        "--max-utilisation", type=float, default=0.8, help="Target committed-lane utilisation."
    )
    parser.add_argument(
        "--budget-video-s", type=float, default=1650.0, help="Video-seconds budget ceiling (§11)."
    )
    parser.add_argument(
        "--cache-hit-ratio", type=float, default=0.0, help="Shot-cache hit ratio (§8.7)."
    )
    parser.add_argument(
        "--session-s", type=float, default=300.0, help="Target per-reader session length (s)."
    )
    parser.add_argument(
        "--high-watermark-s", type=float, default=75.0, help="High watermark H (§4.5)."
    )
    return parser


def plan(args: argparse.Namespace) -> dict[str, object]:
    """Compute the capacity plan as a JSON-friendly document."""
    profile = ReadingProfile(
        velocity_wps=args.velocity_wps,
        seconds_per_shot=args.seconds_per_shot,
        active_fraction=args.active_fraction,
    )
    demand = RenderDemand(readers=args.readers, profile=profile)
    arrival = demand.arrival_rate_shots_per_s
    servers = min_servers_for_utilisation(
        arrival_rate_per_s=arrival,
        service_time_s=args.render_latency_s,
        max_utilisation=args.max_utilisation,
    )
    queue = mmc_queue(
        arrival_rate_per_s=arrival, service_time_s=args.render_latency_s, servers=servers
    )
    feas = watermark_feasibility(
        servers=servers,
        service_time_s=args.render_latency_s,
        seconds_per_shot=args.seconds_per_shot,
        profile=profile,
        high_watermark_s=args.high_watermark_s,
    )
    runway = BudgetRunway(
        ceiling_video_s=args.budget_video_s,
        burn_rate_video_s_per_s=demand.offered_video_seconds_per_s,
        cache_hit_ratio=args.cache_hit_ratio,
    )
    return {
        "inputs": {
            "readers": args.readers,
            "velocity_wps": args.velocity_wps,
            "active_fraction": args.active_fraction,
            "render_latency_s": args.render_latency_s,
            "seconds_per_shot": args.seconds_per_shot,
            "budget_video_s": args.budget_video_s,
            "cache_hit_ratio": args.cache_hit_ratio,
        },
        "demand": {
            "arrival_rate_shots_per_s": round(arrival, 5),
            "offered_video_seconds_per_s": round(demand.offered_video_seconds_per_s, 4),
            "per_reader_video_s_per_wallclock": round(
                profile.video_seconds_per_wallclock, 4
            ),
        },
        "workers": {
            "recommended_committed_slots": servers,
            "queue": queue.to_dict(),
        },
        "watermark": {
            "production_video_s_per_s": round(feas.production_video_s_per_s, 4),
            "consumption_video_s_per_s": round(feas.consumption_video_s_per_s, 4),
            "feasible": feas.feasible,
            "headroom_ratio": round(feas.headroom_ratio, 3),
        },
        "budget": {
            "runway_seconds": round(runway.runway_seconds, 1)
            if runway.runway_seconds != float("inf")
            else "inf",
            "max_concurrent_readers_for_session": max_concurrent_readers(
                ceiling_video_s=args.budget_video_s,
                profile=profile,
                target_session_s=args.session_s,
                cache_hit_ratio=args.cache_hit_ratio,
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    print(json.dumps(plan(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
