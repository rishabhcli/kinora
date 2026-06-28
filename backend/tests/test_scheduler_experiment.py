"""Offline policy A/B tests (kinora.md §13/§4.5/§4.6) — pure, zero video.

Pin :mod:`app.scheduler.experiment`: a policy materialises to settings without
touching the budget/live-gate; scoring a policy over the archetype suite spends
zero video; an A/B reports sensible deltas (a deeper-buffer treatment is at least
as smooth as the baseline); the whole run is deterministic.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.experiment import (
    ABResult,
    default_trace_suite,
    run_ab,
    score_policy,
)
from app.scheduler.policy import SchedulerPolicy
from tests.test_scheduler_support import BOOK_ID, FakeShots, build_shots

_SETTINGS = get_settings()


def _shots() -> FakeShots:
    return FakeShots(build_shots(600, spacing=10, duration_s=5.0))


# --- policy → settings (no budget/gate change) ----------------------------- #


def test_policy_materialises_watermarks_only() -> None:
    base = _SETTINGS
    policy = SchedulerPolicy.from_settings(base).with_(name="deep", low_s=30.0, high_s=90.0)
    settings = policy.to_settings(base)
    assert settings.watermark_low_s == 30.0
    assert settings.watermark_high_s == 90.0
    # Budget + live-gate are copied verbatim → zero-spend invariant preserved.
    assert settings.budget_ceiling_video_s == base.budget_ceiling_video_s
    assert settings.kinora_live_video == base.kinora_live_video


def test_baseline_policy_reproduces_section_4_5_constants() -> None:
    p = SchedulerPolicy.from_settings(_SETTINGS)
    assert (p.low_s, p.high_s, p.commit_horizon_s) == (
        _SETTINGS.watermark_low_s,
        _SETTINGS.watermark_high_s,
        _SETTINGS.commit_horizon_s,
    )


# --- scoring one policy ---------------------------------------------------- #


async def test_score_policy_is_zero_video() -> None:
    p = SchedulerPolicy.from_settings(_SETTINGS, name="baseline")
    report = await score_policy(p, shots=_shots(), book_id=BOOK_ID, base_settings=_SETTINGS)
    assert report.policy == "baseline"
    assert len(report.scores) == len(default_trace_suite())
    assert report.total_video_seconds_spent == 0.0
    assert report.total_simulated_earmarks_s > 0.0  # real promotions occurred


# --- A/B comparison -------------------------------------------------------- #


async def test_ab_deeper_buffer_is_at_least_as_smooth() -> None:
    base = _SETTINGS
    control = SchedulerPolicy.from_settings(base, name="baseline")
    treatment = control.with_(name="deep", low_s=35.0, high_s=95.0)
    result: ABResult = await run_ab(
        control, treatment, shots=_shots(), book_id=BOOK_ID, base_settings=base
    )
    # Both arms render nothing.
    assert result.control.total_video_seconds_spent == 0.0
    assert result.treatment.total_video_seconds_spent == 0.0
    # A deeper buffer should not *increase* stalls and not *decrease* smoothness.
    assert result.delta_stalls <= 0
    assert result.delta_fraction_above_low >= -1e-9
    # Deeper buffer costs more would-be committed video (the budget trade-off).
    assert result.delta_earmarks_s >= 0.0


async def test_ab_is_deterministic() -> None:
    base = _SETTINGS
    control = SchedulerPolicy.from_settings(base, name="a")
    treatment = control.with_(name="b", high_s=90.0)
    r1 = await run_ab(control, treatment, shots=_shots(), book_id=BOOK_ID, base_settings=base)
    r2 = await run_ab(control, treatment, shots=_shots(), book_id=BOOK_ID, base_settings=base)
    assert r1.summary() == r2.summary()


async def test_ab_summary_shape() -> None:
    base = _SETTINGS
    control = SchedulerPolicy.from_settings(base, name="ctrl")
    treatment = control.with_(name="trt", high_s=80.0)
    result = await run_ab(control, treatment, shots=_shots(), book_id=BOOK_ID, base_settings=base)
    summary = result.summary()
    assert summary["control"] == "ctrl"
    assert summary["treatment"] == "trt"
    assert set(summary.keys()) >= {
        "delta_fraction_above_low",
        "delta_stalls",
        "delta_earmarks_s",
        "control_video_spent",
        "treatment_video_spent",
    }
    assert summary["control_video_spent"] == 0.0
    assert summary["treatment_video_spent"] == 0.0
