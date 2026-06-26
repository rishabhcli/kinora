"""Zone/ETA/stability math (kinora.md §4.3/§4.4/§4.6) — pure, no infra."""

from __future__ import annotations

from dataclasses import dataclass

from app.scheduler.zones import (
    DEFAULT_VELOCITY_WPS,
    VELOCITY_CLAMP_HIGH,
    VELOCITY_CLAMP_LOW,
    Zone,
    clamp_velocity,
    classify,
    classify_shot,
    eta_seconds,
    trajectory_is_stable,
    viewer_zone,
)

_COMMIT_H = 8.0
_SPEC_H = 20.0


def _zone(eta: float | None, *, stable: bool = True, budget_ok: bool = True) -> Zone:
    return viewer_zone(
        eta, stable=stable, budget_ok=budget_ok, commit_horizon_s=_COMMIT_H, spec_horizon_s=_SPEC_H
    )


def test_viewer_zone_none_eta_is_cold() -> None:
    assert _zone(None) is Zone.COLD


def test_viewer_zone_near_shot_is_committed_when_stable_and_funded() -> None:
    assert _zone(2.0, stable=True, budget_ok=True) is Zone.COMMITTED


def test_viewer_zone_beyond_spec_horizon_is_cold() -> None:
    assert _zone(_SPEC_H + 1.0) is Zone.COLD


def test_viewer_zone_between_horizons_is_speculative() -> None:
    assert _zone((_COMMIT_H + _SPEC_H) / 2) is Zone.SPECULATIVE


def test_viewer_zone_near_shot_demotes_to_speculative_when_skimming() -> None:
    # Inside the commit horizon but unstable → ride the keyframe ladder, not video.
    assert _zone(2.0, stable=False, budget_ok=True) is Zone.SPECULATIVE


def test_viewer_zone_near_shot_demotes_to_speculative_when_budget_pressured() -> None:
    assert _zone(2.0, stable=True, budget_ok=False) is Zone.SPECULATIVE


def test_viewer_zone_far_shot_stays_speculative_regardless_of_gates() -> None:
    # The demotion only applies to would-be COMMITTED shots; a SPECULATIVE-zone
    # shot is unaffected by stability/budget.
    mid = (_COMMIT_H + _SPEC_H) / 2
    assert _zone(mid, stable=False, budget_ok=False) is Zone.SPECULATIVE


@dataclass
class _Traj:
    raw_velocity_wps: float
    oscillating: bool = False


def test_eta_divides_distance_by_velocity() -> None:
    # 120 words ahead at 4 wps = 30 reading-seconds.
    assert eta_seconds(120, 0, 4.0) == 30.0
    # Faster reader => nearer in time (self-tuning, §4.6).
    assert eta_seconds(120, 0, 8.0) == 15.0
    # A shot behind the focus word has a negative ETA.
    assert eta_seconds(0, 120, 4.0) == -30.0


def test_eta_never_divides_by_zero() -> None:
    assert eta_seconds(100, 0, 0.0) > 0  # clamped to a tiny floor, no ZeroDivision


def test_classify_three_zones() -> None:
    assert classify(10, commit_horizon_s=45, spec_horizon_s=240) is Zone.COMMITTED
    assert classify(45, commit_horizon_s=45, spec_horizon_s=240) is Zone.SPECULATIVE
    assert classify(200, commit_horizon_s=45, spec_horizon_s=240) is Zone.SPECULATIVE
    assert classify(241, commit_horizon_s=45, spec_horizon_s=240) is Zone.COLD


def test_classify_shot_combines_eta_and_zone() -> None:
    eta, zone = classify_shot(360, 0, 4.0, commit_horizon_s=45, spec_horizon_s=240)
    assert eta == 90.0 and zone is Zone.SPECULATIVE


def test_velocity_clamp_band() -> None:
    assert clamp_velocity(0.1) == VELOCITY_CLAMP_LOW  # 2.0
    assert clamp_velocity(100.0) == VELOCITY_CLAMP_HIGH  # 12.0
    assert clamp_velocity(DEFAULT_VELOCITY_WPS) == DEFAULT_VELOCITY_WPS


def test_trajectory_stability() -> None:
    assert trajectory_is_stable(_Traj(raw_velocity_wps=4.0)) is True
    assert trajectory_is_stable(_Traj(raw_velocity_wps=12.0)) is True  # at the ceiling = fast read
    # Above the clamp ceiling = rapid skim -> suspend promotion (§4.6).
    assert trajectory_is_stable(_Traj(raw_velocity_wps=20.0)) is False
    # Oscillating direction -> unstable regardless of speed.
    assert trajectory_is_stable(_Traj(raw_velocity_wps=4.0, oscillating=True)) is False
