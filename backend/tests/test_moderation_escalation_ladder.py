"""Unit tests for the escalation ladder (pure tier computation + window decay)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.moderation.escalation import (
    EnforcementTier,
    EscalationPolicy,
    compute_tier,
    window_expired,
)
from app.moderation.taxonomy import Severity

P = EscalationPolicy(
    warn_at=1, throttle_at=3, suspend_at=5, ban_at=8, window=timedelta(hours=24)
)


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, EnforcementTier.CLEAN),
        (1, EnforcementTier.WARNED),
        (2, EnforcementTier.WARNED),
        (3, EnforcementTier.THROTTLED),
        (4, EnforcementTier.THROTTLED),
        (5, EnforcementTier.SUSPENDED),
        (7, EnforcementTier.SUSPENDED),
        (8, EnforcementTier.BANNED),
        (20, EnforcementTier.BANNED),
    ],
)
def test_compute_tier_crosses_thresholds(count: int, expected: EnforcementTier) -> None:
    assert compute_tier(count, P) is expected


def test_tiers_are_ordered() -> None:
    assert (
        EnforcementTier.CLEAN
        < EnforcementTier.WARNED
        < EnforcementTier.THROTTLED
        < EnforcementTier.SUSPENDED
        < EnforcementTier.BANNED
    )


def test_severity_weighting() -> None:
    policy = EscalationPolicy(severity_weight=3)
    assert policy.weight_for(Severity.CRITICAL) == 3
    assert policy.weight_for(Severity.HIGH) == 2
    assert policy.weight_for(Severity.MEDIUM) == 1
    assert policy.weight_for(Severity.LOW) == 1


def test_severity_weight_floor_is_one() -> None:
    policy = EscalationPolicy(severity_weight=1)
    assert policy.weight_for(Severity.CRITICAL) >= 1
    assert policy.weight_for(Severity.HIGH) >= 1


def test_window_expiry() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert not window_expired(start, start + timedelta(hours=23), P)
    assert window_expired(start, start + timedelta(hours=24), P)
    assert window_expired(start, start + timedelta(hours=48), P)


def test_window_expiry_handles_naive_datetime() -> None:
    # A naive (tz-less) window start is treated as UTC, not crashed.
    start = datetime(2026, 1, 1)  # noqa: DTZ001 - intentional naive input
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert window_expired(start, now, P)
