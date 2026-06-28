"""``python -m app.scheduler.run`` — offline buffer-health / policy-A/B report.

The operator entrypoint for the predictive-prefetch proof. Unlike the §13 eval
runner (:mod:`app.eval.run`), this needs **no infra**: it builds an in-memory
source-span index and replays the :class:`~app.scheduler.simulation.ReaderProfile`
archetype suite through the real Scheduler under one or two
:class:`~app.scheduler.policy.SchedulerPolicy` arms, printing the §13 buffer-health
numbers (fraction above ``L``, visible stalls) and the would-be committed video.

It spends **zero video-seconds** by construction (the harness renders nothing and
the budget gate is the §4.4 dry-run gate), so it is safe to run anywhere, anytime,
without a DashScope key or a live stack. Examples::

    python -m app.scheduler.run                       # baseline over the suite
    python -m app.scheduler.run --high 90 --low 35    # A/B baseline vs deeper buffer
    python -m app.scheduler.run --shots 800 --json    # machine-readable output

This is intentionally a thin shell over :func:`app.scheduler.experiment.run_ab` /
:func:`app.scheduler.experiment.score_policy` — the same code paths the unit suite
pins — so the printed numbers are exactly what the harness asserts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.scheduler.experiment import ArmReport, run_ab, score_policy
from app.scheduler.policy import SchedulerPolicy
from app.scheduler.service import SchedulerShot, ShotSource

_BOOK_ID = "book_sim"


@dataclass
class _SimShot:
    """A minimal in-memory shot for the offline span index (mirrors FakeShot)."""

    id: str
    beat_id: str | None
    scene_id: str | None
    word_index_start: int
    duration_s: float | None = 5.0
    prompt: str | None = None
    source_span: dict[str, Any] | None = None


class _SimShots(ShotSource):
    """An in-memory §4.2 source-span index built from evenly spaced shots."""

    def __init__(self, count: int, *, spacing: int = 10, duration_s: float = 5.0) -> None:
        self._shots: list[_SimShot] = [
            _SimShot(
                id=f"shot_{i:04d}",
                beat_id=f"beat_{i:04d}",
                scene_id="scene_001",
                word_index_start=i * spacing,
                duration_s=duration_s,
                prompt=f"beat {i}",
            )
            for i in range(1, count + 1)
        ]

    async def next_uncommitted_shot(
        self, book_id: str, after_word: int
    ) -> SchedulerShot | None:
        for shot in self._shots:
            if shot.word_index_start > after_word:
                return shot
        return None

    async def resolve_word_to_shot(
        self, book_id: str, word_index: int
    ) -> SchedulerShot | None:
        found: _SimShot | None = None
        for shot in self._shots:
            if shot.word_index_start <= word_index:
                found = shot
            else:
                break
        return found


def _report_dict(report: ArmReport) -> dict[str, Any]:
    return {
        "policy": report.policy,
        "mean_fraction_above_low": round(report.mean_fraction_above_low, 4),
        "total_stalls": report.total_stalls,
        "total_simulated_earmarks_s": report.total_simulated_earmarks_s,
        "total_video_seconds_spent": report.total_video_seconds_spent,
        "traces": [
            {
                "label": s.label,
                "fraction_above_low": round(s.fraction_above_low, 4),
                "stalls": s.stalls,
                "peak_committed_s": round(s.peak_committed_s, 2),
                "promotions": s.committed_promotions,
                "keyframes": s.keyframes_ensured,
                "earmarks_s": round(s.simulated_earmarks_s, 2),
            }
            for s in report.scores
        ],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    base = get_settings()
    shots = _SimShots(args.shots)
    control = SchedulerPolicy.from_settings(base, name="baseline")

    treats_requested = args.low is not None or args.high is not None or args.commit is not None
    if not treats_requested:
        report = await score_policy(control, shots=shots, book_id=_BOOK_ID, base_settings=base)
        return {"arms": [_report_dict(report)]}

    treatment = control.with_(
        name="treatment",
        low_s=args.low if args.low is not None else control.low_s,
        high_s=args.high if args.high is not None else control.high_s,
        commit_horizon_s=args.commit if args.commit is not None else control.commit_horizon_s,
    )
    result = await run_ab(control, treatment, shots=shots, book_id=_BOOK_ID, base_settings=base)
    return {
        "arms": [_report_dict(result.control), _report_dict(result.treatment)],
        "deltas": result.summary(),
    }


def _print_human(out: dict[str, Any]) -> None:
    for arm in out["arms"]:
        print(f"\n=== policy: {arm['policy']} ===")
        print(
            f"  mean fraction-above-L: {arm['mean_fraction_above_low']:.3f}"
            f"   stalls: {arm['total_stalls']}"
            f"   would-be video: {arm['total_simulated_earmarks_s']:.0f}s"
            f"   REAL video spent: {arm['total_video_seconds_spent']:.1f}s"
        )
        for t in arm["traces"]:
            print(
                f"    {t['label']:<24} above-L={t['fraction_above_low']:.3f}"
                f" stalls={t['stalls']} peak={t['peak_committed_s']:.0f}s"
                f" promos={t['promotions']} kf={t['keyframes']}"
            )
    if "deltas" in out:
        d = out["deltas"]
        print("\n=== A/B (treatment − control) ===")
        print(
            f"  Δ fraction-above-L: {d['delta_fraction_above_low']:+.4f}"
            f"   Δ stalls: {d['delta_stalls']:+d}"
            f"   Δ would-be video: {d['delta_earmarks_s']:+.0f}s"
        )
    print("\n(zero real video-seconds spent — offline harness, budget gate dry-run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline scheduler buffer-health / A/B report.")
    parser.add_argument("--shots", type=int, default=600, help="in-memory shot count")
    parser.add_argument("--low", type=float, default=None, help="treatment low watermark L (s)")
    parser.add_argument("--high", type=float, default=None, help="treatment high watermark H (s)")
    parser.add_argument(
        "--commit", type=float, default=None, help="treatment commit horizon C (s)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args(argv)

    out = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _print_human(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
