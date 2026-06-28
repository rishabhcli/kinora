"""Unit tests for the 5-field cron engine (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.jobs.cron import CronError, parse_cron


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_every_minute() -> None:
    sched = parse_cron("* * * * *")
    nxt = sched.next_after(at(2026, 1, 1, 0, 0, ))
    assert nxt == at(2026, 1, 1, 0, 1)


def test_specific_minute_and_hour() -> None:
    sched = parse_cron("30 3 * * *")
    nxt = sched.next_after(at(2026, 1, 1, 0, 0))
    assert nxt == at(2026, 1, 1, 3, 30)
    # next day after it has passed
    nxt2 = sched.next_after(at(2026, 1, 1, 3, 30))
    assert nxt2 == at(2026, 1, 2, 3, 30)


def test_step_field() -> None:
    sched = parse_cron("*/15 * * * *")
    assert sched.next_after(at(2026, 1, 1, 0, 0)) == at(2026, 1, 1, 0, 15)
    assert sched.next_after(at(2026, 1, 1, 0, 16)) == at(2026, 1, 1, 0, 30)


def test_range_field() -> None:
    sched = parse_cron("0 9-17 * * *")
    assert sched.next_after(at(2026, 1, 1, 8, 30)) == at(2026, 1, 1, 9, 0)
    assert sched.next_after(at(2026, 1, 1, 17, 0)) == at(2026, 1, 2, 9, 0)


def test_comma_list() -> None:
    sched = parse_cron("0,30 * * * *")
    assert sched.next_after(at(2026, 1, 1, 0, 5)) == at(2026, 1, 1, 0, 30)
    assert sched.next_after(at(2026, 1, 1, 0, 45)) == at(2026, 1, 1, 1, 0)


def test_day_of_week_sunday_zero_and_seven_equivalent() -> None:
    # 2026-01-04 is a Sunday.
    by0 = parse_cron("0 0 * * 0")
    by7 = parse_cron("0 0 * * 7")
    nxt0 = by0.next_after(at(2026, 1, 1))
    nxt7 = by7.next_after(at(2026, 1, 1))
    assert nxt0 == nxt7 == at(2026, 1, 4)


def test_dom_and_dow_or_semantics() -> None:
    # Restricted DOM (15th) OR DOW (Monday) -> matches on EITHER.
    sched = parse_cron("0 0 15 * 1")
    # 2026-01-05 is the first Monday on/after Jan 1.
    nxt = sched.next_after(at(2026, 1, 1))
    assert nxt == at(2026, 1, 5)  # Monday comes before the 15th
    # After Jan 5, the next match is the 12th (next Monday), not the 15th.
    assert sched.next_after(at(2026, 1, 5)) == at(2026, 1, 12)


def test_dom_only_restricted_uses_and() -> None:
    sched = parse_cron("0 0 1 * *")  # first of every month
    assert sched.next_after(at(2026, 1, 2)) == at(2026, 2, 1)


def test_month_field() -> None:
    sched = parse_cron("0 0 1 7 *")  # July 1
    assert sched.next_after(at(2026, 1, 1)) == at(2026, 7, 1)


def test_matches_predicate() -> None:
    sched = parse_cron("30 3 * * *")
    assert sched.matches(at(2026, 1, 1, 3, 30))
    assert not sched.matches(at(2026, 1, 1, 3, 31))


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",  # too few fields
        "* * * * * *",  # too many
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # dom below 1
        "* * * 13 *",  # month out of range
        "5-2 * * * *",  # descending range
        "*/0 * * * *",  # zero step
        "a * * * *",  # non-numeric
        ", * * * *",  # empty term
    ],
)
def test_invalid_expressions_raise(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)


def test_expression_round_trips_on_schedule() -> None:
    sched = parse_cron("0 3 * * *")
    assert sched.expression == "0 3 * * *"
