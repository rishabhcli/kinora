"""Kill-switch safety — a guarded flag can never be raised upward (pure)."""

from __future__ import annotations

import pytest

from app.flags.plane.errors import KillSwitchViolation
from app.flags.plane.safety import KillSwitchGuard
from app.flags.plane.spec import FlagSpec, FlagType

GUARD = KillSwitchGuard()


def _bool_ks(default: bool) -> FlagSpec:
    return FlagSpec(key="kinora.live_video", type=FlagType.BOOL, default=default, kill_switch=True)


def test_non_kill_switch_always_safe() -> None:
    spec = FlagSpec(key="x", type=FlagType.BOOL, default=False)
    assert GUARD.is_safe(spec, False, True) is True  # ordinary flag can be raised
    GUARD.check(spec, False, True)  # does not raise


def test_bool_kill_switch_cannot_be_raised_from_off() -> None:
    spec = _bool_ks(False)
    assert GUARD.is_safe(spec, False, True) is False
    with pytest.raises(KillSwitchViolation):
        GUARD.check(spec, False, True)


def test_bool_kill_switch_can_be_forced_off() -> None:
    spec = _bool_ks(True)  # base happens to be on
    assert GUARD.is_safe(spec, True, False) is True  # forcing further OFF is safe
    GUARD.check(spec, True, False)


def test_bool_kill_switch_clamp_pins_to_base() -> None:
    spec = _bool_ks(False)
    # A value that would raise the switch is silently clamped back to the base.
    assert GUARD.clamp(spec, False, True) is False
    # A safe value passes through.
    assert GUARD.clamp(spec, False, False) is False


def test_numeric_kill_switch_lower_is_safe() -> None:
    spec = FlagSpec(key="budget.ceiling_usd", type=FlagType.FLOAT, default=30.0, kill_switch=True)
    assert GUARD.is_safe(spec, 30.0, 10.0) is True  # lowering the cap is safe
    assert GUARD.is_safe(spec, 30.0, 50.0) is False  # raising the cap is not
    assert GUARD.clamp(spec, 30.0, 50.0) == 30.0


def test_string_kill_switch_only_equal_is_safe() -> None:
    spec = FlagSpec(key="mode", type=FlagType.STRING, default="safe", kill_switch=True)
    assert GUARD.is_safe(spec, "safe", "safe") is True
    assert GUARD.is_safe(spec, "safe", "risky") is False


def test_none_candidate_always_safe() -> None:
    spec = _bool_ks(False)
    assert GUARD.is_safe(spec, False, None) is True
