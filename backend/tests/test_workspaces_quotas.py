"""Pure-unit tests for the quota + seat policy helpers (no infra)."""

from __future__ import annotations

import pytest

from app.workspaces.quotas import (
    SETTING_DEFAULT_MEMBER_ROLE,
    SETTING_MAX_BOOKS,
    QuotaExceeded,
    SeatUsage,
    check_book_quota,
    check_seat_quota,
    default_member_role_for,
    default_seats_for_plan,
    max_books_for,
    video_seconds_cap_for,
)
from app.workspaces.roles import OrgPlan, Role


def test_default_seats_for_plan() -> None:
    assert default_seats_for_plan(OrgPlan.FREE) == 3
    assert default_seats_for_plan(OrgPlan.TEAM) == 25
    assert default_seats_for_plan(OrgPlan.ENTERPRISE) == 0  # unlimited


def test_max_books_for_parsing() -> None:
    assert max_books_for(None) is None
    assert max_books_for({}) is None
    assert max_books_for({SETTING_MAX_BOOKS: 0}) is None  # 0 == unlimited
    assert max_books_for({SETTING_MAX_BOOKS: -5}) is None
    assert max_books_for({SETTING_MAX_BOOKS: "10"}) == 10
    assert max_books_for({SETTING_MAX_BOOKS: 42}) == 42
    assert max_books_for({SETTING_MAX_BOOKS: "nope"}) is None


def test_video_seconds_cap_for() -> None:
    assert video_seconds_cap_for({"video_seconds_cap": 1650}) == 1650
    assert video_seconds_cap_for(None) is None


def test_default_member_role_for() -> None:
    assert default_member_role_for(None) == Role.VIEWER
    assert default_member_role_for({SETTING_DEFAULT_MEMBER_ROLE: "editor"}) == Role.EDITOR
    assert default_member_role_for({SETTING_DEFAULT_MEMBER_ROLE: "bogus"}) == Role.VIEWER


def test_check_book_quota() -> None:
    # No cap -> always fine.
    check_book_quota(None, current_books=1000)
    check_book_quota({SETTING_MAX_BOOKS: 0}, current_books=1000)
    # Cap of 3: at 2 is fine, at 3 is over.
    check_book_quota({SETTING_MAX_BOOKS: 3}, current_books=2)
    with pytest.raises(QuotaExceeded) as ei:
        check_book_quota({SETTING_MAX_BOOKS: 3}, current_books=3)
    assert ei.value.quota == SETTING_MAX_BOOKS
    assert ei.value.limit == 3


def test_seat_usage_unlimited() -> None:
    usage = SeatUsage(seats=0, active_members=999)
    assert usage.unlimited is True
    assert usage.can_add(50) is True
    check_seat_quota(usage, adding=100)  # never raises


def test_seat_usage_limited() -> None:
    usage = SeatUsage(seats=3, active_members=2)
    assert usage.unlimited is False
    assert usage.available == 1
    assert usage.can_add(1) is True
    assert usage.can_add(2) is False
    check_seat_quota(usage, adding=1)
    with pytest.raises(QuotaExceeded) as ei:
        check_seat_quota(usage, adding=2)
    assert ei.value.quota == "seats"


def test_seat_usage_full() -> None:
    usage = SeatUsage(seats=5, active_members=5)
    assert usage.available == 0
    assert usage.can_add(1) is False
