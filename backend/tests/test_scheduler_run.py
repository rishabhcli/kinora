"""Offline scheduler-run CLI tests (kinora.md §13/§4.5) — pure, zero video.

Pin :mod:`app.scheduler.run`: the in-memory span index drives the archetype suite,
a single-arm run reports buffer health with zero real video, and an A/B run (any
watermark override) reports both arms + deltas. No infra, no DashScope key.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.run import _run, _SimShots
from app.scheduler.service import _shot_start

_SETTINGS = get_settings()


class _Args:
    def __init__(self, **kw: object) -> None:
        self.shots = 400
        self.low = None
        self.high = None
        self.commit = None
        self.json = False
        for k, v in kw.items():
            setattr(self, k, v)


async def test_sim_shots_index_is_ordered() -> None:
    shots = _SimShots(50, spacing=10)
    first = await shots.next_uncommitted_shot("book_sim", 0)
    assert first is not None and _shot_start(first) == 10
    resolved = await shots.resolve_word_to_shot("book_sim", 95)
    assert resolved is not None and _shot_start(resolved) <= 95


async def test_single_arm_run_is_zero_video() -> None:
    out = await _run(_Args())  # type: ignore[arg-type]
    assert len(out["arms"]) == 1
    arm = out["arms"][0]
    assert arm["policy"] == "baseline"
    assert arm["total_video_seconds_spent"] == 0.0
    assert arm["total_simulated_earmarks_s"] > 0.0
    assert arm["traces"]  # the archetype suite ran


async def test_ab_run_reports_both_arms_and_deltas() -> None:
    out = await _run(_Args(high=95.0, low=35.0))  # type: ignore[arg-type]
    assert len(out["arms"]) == 2
    assert {a["policy"] for a in out["arms"]} == {"baseline", "treatment"}
    assert out["arms"][0]["total_video_seconds_spent"] == 0.0
    assert out["arms"][1]["total_video_seconds_spent"] == 0.0
    deltas = out["deltas"]
    assert "delta_fraction_above_low" in deltas
    assert deltas["control_video_spent"] == 0.0
    assert deltas["treatment_video_spent"] == 0.0
