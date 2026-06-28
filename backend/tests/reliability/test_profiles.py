"""Unit tests for named run profiles (app.reliability.profiles)."""

from __future__ import annotations

import pytest

from app.reliability.profiles import (
    ProfileOverrides,
    get_profile,
    profile_registry,
)
from app.reliability.workload import WorkloadKind


def test_registry_has_expected_profiles() -> None:
    names = set(profile_registry())
    assert {"steady_soak", "skim_storm", "seek_thrash", "open_spike", "cold_open"} <= names


def test_get_profile_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("nope")


def test_steady_soak_is_closed_with_overrides() -> None:
    profile = get_profile("steady_soak")
    assert profile.workload_kind is WorkloadKind.CLOSED
    plan = profile.build_workload(ProfileOverrides(users=24, duration_s=90.0))
    assert plan.kind is WorkloadKind.CLOSED
    assert plan.closed_model is not None
    assert plan.closed_model.users == 24
    assert plan.duration_s == 90.0


def test_open_spike_uses_rate_or_users() -> None:
    profile = get_profile("open_spike")
    assert profile.workload_kind is WorkloadKind.OPEN
    # Explicit rate wins.
    plan = profile.build_workload(ProfileOverrides(users=8, duration_s=30.0, rate_rps=50.0))
    assert plan.open_model is not None
    assert plan.open_model.base_rate_rps == 50.0
    # Falls back to users when rate is 0.
    plan2 = profile.build_workload(ProfileOverrides(users=8, duration_s=30.0, rate_rps=0.0))
    assert plan2.open_model is not None
    assert plan2.open_model.base_rate_rps == 8.0


def test_each_profile_resolves_a_scenario_and_slos() -> None:
    for name, profile in profile_registry().items():
        scenario = profile.scenario()
        assert scenario.name == profile.scenario_name
        assert profile.slos.slos  # non-empty SLO set
        plan = profile.build_workload(ProfileOverrides(users=4, duration_s=10.0))
        assert plan.duration_s == 10.0, name


def test_cold_open_has_no_warmup() -> None:
    profile = get_profile("cold_open")
    plan = profile.build_workload(ProfileOverrides(users=10, duration_s=20.0))
    assert plan.closed_model is not None
    # Step ramp at t=0 with floor 1.0 => all users active immediately.
    assert plan.closed_model.active_users(0.0) == 10


def test_profile_descriptions_present() -> None:
    for profile in profile_registry().values():
        assert profile.description
