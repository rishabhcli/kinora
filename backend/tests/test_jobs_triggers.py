"""Unit tests for triggers (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.jobs.triggers import (
    CronTrigger,
    IntervalTrigger,
    ManualTrigger,
    OnceTrigger,
    Trigger,
    cron,
    every,
    manual,
    once,
)
from app.jobs.types import TriggerKind


def t(h: int, mi: int = 0) -> datetime:
    return datetime(2026, 1, 1, h, mi, tzinfo=UTC)


def test_all_triggers_satisfy_protocol() -> None:
    for trig in (cron("* * * * *"), every(30), once(t(1)), manual()):
        assert isinstance(trig, Trigger)


def test_cron_trigger_kind_and_fire() -> None:
    trig = CronTrigger.parse("0 * * * *")
    assert trig.kind is TriggerKind.CRON
    assert trig.next_fire(after=t(0, 5), last_fire=None) == t(1, 0)


def test_cron_trigger_repr_shows_expression() -> None:
    assert "0 * * * *" in repr(cron("0 * * * *"))


def test_interval_without_anchor_first_fire_is_after_plus_step() -> None:
    trig = every(60)
    assert trig.kind is TriggerKind.INTERVAL
    assert trig.next_fire(after=t(0, 0), last_fire=None) == t(0, 1)


def test_interval_subsequent_fire_relative_to_last() -> None:
    trig = every(60)
    nxt = trig.next_fire(after=t(0, 1), last_fire=t(0, 1))
    assert nxt == t(0, 2)


def test_interval_catches_up_when_behind() -> None:
    # last fired at 00:00, step 60s, but ``after`` is already 00:05 (loop paused).
    trig = every(60)
    nxt = trig.next_fire(after=t(0, 5), last_fire=t(0, 0))
    assert nxt == t(0, 6)  # smallest grid point strictly after 00:05


def test_interval_with_anchor_is_wall_clock_grid() -> None:
    anchor = t(0, 0)
    trig = IntervalTrigger(seconds=300, anchor=anchor)  # every 5 min on the grid
    assert trig.next_fire(after=t(0, 2), last_fire=None) == t(0, 5)
    assert trig.next_fire(after=t(0, 5), last_fire=t(0, 5)) == t(0, 10)


def test_interval_with_future_anchor_fires_at_anchor() -> None:
    anchor = t(1, 0)
    trig = IntervalTrigger(seconds=300, anchor=anchor)
    assert trig.next_fire(after=t(0, 0), last_fire=None) == anchor


def test_interval_rejects_nonpositive() -> None:
    with pytest.raises(ValueError, match="positive"):
        IntervalTrigger(seconds=0)


def test_once_fires_once_then_never() -> None:
    trig = once(t(1, 0))
    assert trig.kind is TriggerKind.ONCE
    assert trig.next_fire(after=t(0, 0), last_fire=None) == t(1, 0)
    # already fired
    assert trig.next_fire(after=t(2, 0), last_fire=t(1, 0)) is None


def test_once_in_the_past_fires_immediately() -> None:
    trig = once(t(0, 0))
    # ``at`` is not strictly after ``after`` -> fire at ``after`` (now).
    fire = trig.next_fire(after=t(1, 0), last_fire=None)
    assert fire == t(1, 0)


def test_manual_never_fires() -> None:
    trig = manual()
    assert trig.kind is TriggerKind.MANUAL
    assert trig.next_fire(after=t(0, 0), last_fire=None) is None
    assert isinstance(trig, ManualTrigger)


def test_once_trigger_strictly_future() -> None:
    base = t(0, 0)
    trig = OnceTrigger(at=base + timedelta(hours=2))
    assert trig.next_fire(after=base, last_fire=None) == base + timedelta(hours=2)
