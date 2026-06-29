"""Serde round-trip tests — payloads must persist losslessly + canonically.

The durable boundary relies on :mod:`app.platform.workflows.serde` turning
arbitrary workflow/activity payloads into JSON-native, *canonical* (sorted-key)
bytes that round-trip unchanged. If serialisation weren't deterministic, replay
would diverge — so these tests pin both losslessness and canonical ordering.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.platform.workflows.serde import dumps, from_jsonable, to_jsonable


class Color(enum.StrEnum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Point:
    x: int
    y: int


def test_scalars_round_trip() -> None:
    for value in [None, True, False, 0, -3, 3.14, "hello", ""]:
        assert from_jsonable(to_jsonable(value)) == value


def test_datetime_round_trips_to_aware_datetime() -> None:
    dt = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
    assert from_jsonable(to_jsonable(dt)) == dt


def test_decimal_round_trips_losslessly() -> None:
    d = Decimal("1234.5678")
    assert from_jsonable(to_jsonable(d)) == d


def test_enum_serialises_to_value() -> None:
    assert to_jsonable(Color.RED) == "red"


def test_dataclass_serialises_to_field_dict() -> None:
    assert to_jsonable(Point(1, 2)) == {"x": 1, "y": 2}


def test_set_becomes_sorted_list() -> None:
    assert to_jsonable({3, 1, 2}) == [1, 2, 3]


def test_tuple_becomes_list() -> None:
    assert to_jsonable((1, 2)) == [1, 2]


def test_nested_structures_round_trip() -> None:
    value = {"b": [1, {"c": Decimal("2.5")}], "a": datetime(2026, 1, 1, tzinfo=UTC)}
    assert from_jsonable(to_jsonable(value)) == value


def test_dumps_is_canonical_sorted_keys() -> None:
    # Two dicts with the same content but different insertion order serialise equal.
    a = dumps({"z": 1, "a": 2})
    b = dumps({"a": 2, "z": 1})
    assert a == b == '{"a":2,"z":1}'


def test_unserialisable_type_raises() -> None:
    with pytest.raises(TypeError):
        to_jsonable(object())
