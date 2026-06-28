"""Unit tests for HTTP byte-range parsing + slicing (pure)."""

from __future__ import annotations

import pytest

from app.media.ranges import ByteRange, RangeNotSatisfiableError, parse_range


def test_no_header_returns_none() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("", 100) is None


def test_non_bytes_unit_returns_none() -> None:
    assert parse_range("items=0-10", 100) is None


def test_full_explicit_range() -> None:
    r = parse_range("bytes=0-49", 100)
    assert r == ByteRange(0, 49, 100)
    assert r.length == 50
    assert r.content_range == "bytes 0-49/100"


def test_open_ended_range_to_eof() -> None:
    r = parse_range("bytes=50-", 100)
    assert r == ByteRange(50, 99, 100)
    assert r.length == 50


def test_suffix_range_last_n_bytes() -> None:
    r = parse_range("bytes=-20", 100)
    assert r == ByteRange(80, 99, 100)
    assert r.length == 20


def test_suffix_larger_than_total_clamps_to_start() -> None:
    r = parse_range("bytes=-500", 100)
    assert r == ByteRange(0, 99, 100)


def test_end_beyond_total_is_clamped() -> None:
    r = parse_range("bytes=90-999", 100)
    assert r == ByteRange(90, 99, 100)


def test_slice_is_inclusive() -> None:
    data = bytes(range(10))
    r = parse_range("bytes=2-5", 10)
    assert r is not None
    assert r.slice(data) == bytes([2, 3, 4, 5])


def test_multi_range_falls_back_to_full_body() -> None:
    assert parse_range("bytes=0-10,20-30", 100) is None


@pytest.mark.parametrize("spec", ["bytes=abc-def", "bytes=10", "bytes=-0"])
def test_malformed_returns_none(spec: str) -> None:
    assert parse_range(spec, 100) is None


def test_start_past_end_of_resource_raises() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_range("bytes=200-300", 100)


def test_start_after_end_raises() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_range("bytes=50-10", 100)


def test_empty_resource_raises() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_range("bytes=0-10", 0)
