"""Unit tests for the report formatting helpers (the deterministic strings)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.reports.format import (
    fmt_date,
    fmt_datetime,
    fmt_duration_clock,
    fmt_float,
    fmt_int,
    fmt_iso,
    fmt_minutes,
    fmt_pct,
    fmt_pct_value,
    fmt_seconds,
    ordinal,
    pluralize,
)


def test_fmt_int_groups_thousands() -> None:
    assert fmt_int(12345) == "12,345"
    assert fmt_int(7.6) == "8"


def test_fmt_float_places_and_grouping() -> None:
    assert fmt_float(1234.5) == "1,234.5"
    assert fmt_float(1.23456, 3) == "1.235"


def test_fmt_pct_from_fraction_and_value() -> None:
    assert fmt_pct(0.873) == "87.3%"
    assert fmt_pct_value(87.3) == "87.3%"


def test_fmt_seconds_scales_to_minutes_and_hours() -> None:
    assert fmt_seconds(42) == "42s"
    assert fmt_seconds(95) == "1m 35s"
    assert fmt_seconds(120) == "2m"
    assert fmt_seconds(3725) == "1h 2m"
    assert fmt_seconds(3600) == "1h"


def test_fmt_minutes_and_clock() -> None:
    assert fmt_minutes(150) == "2.5 min"
    assert fmt_duration_clock(95) == "1:35"
    assert fmt_duration_clock(3725) == "1:02:05"


def test_fmt_dates_are_utc_and_stable() -> None:
    dt = datetime(2026, 6, 28, 14, 5, 30, tzinfo=UTC)
    assert fmt_date(dt) == "28 Jun 2026"
    assert fmt_datetime(dt) == "28 Jun 2026 14:05 UTC"
    assert fmt_iso(dt) == "2026-06-28T14:05:30Z"


def test_naive_datetime_treated_as_utc() -> None:
    dt = datetime(2026, 1, 1, 0, 0, 0)
    assert fmt_date(dt) == "01 Jan 2026"


def test_ordinal_handles_teens_and_units() -> None:
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"
    assert ordinal(21) == "21st"
    assert ordinal(22) == "22nd"


def test_pluralize() -> None:
    assert pluralize(1, "book") == "1 book"
    assert pluralize(3, "book") == "3 books"
    assert pluralize(2, "mouse", "mice") == "2 mice"
    assert pluralize(1000, "word") == "1,000 words"
