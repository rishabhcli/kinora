"""Typed flag spec — coercion, validation, defaults, choices (pure, no infra)."""

from __future__ import annotations

import pytest

from app.flags.plane.errors import FlagTypeError
from app.flags.plane.spec import FlagSpec, FlagType


def test_bool_coercion_accepts_truthy_strings_and_ints() -> None:
    assert FlagType.BOOL.coerce("k", "true") is True
    assert FlagType.BOOL.coerce("k", "off") is False
    assert FlagType.BOOL.coerce("k", 1) is True
    assert FlagType.BOOL.coerce("k", 0) is False


def test_bool_coercion_rejects_nonsense() -> None:
    with pytest.raises(FlagTypeError):
        FlagType.BOOL.coerce("k", "maybe")
    with pytest.raises(FlagTypeError):
        FlagType.BOOL.coerce("k", 2)


def test_int_coercion_rejects_bool_and_float_with_fraction() -> None:
    assert FlagType.INT.coerce("k", "42") == 42
    assert FlagType.INT.coerce("k", 7.0) == 7
    with pytest.raises(FlagTypeError):
        FlagType.INT.coerce("k", True)  # bool is not a valid int here
    with pytest.raises(FlagTypeError):
        FlagType.INT.coerce("k", 1.5)


def test_float_coercion() -> None:
    assert FlagType.FLOAT.coerce("k", 3) == 3.0
    assert FlagType.FLOAT.coerce("k", "2.5") == 2.5
    with pytest.raises(FlagTypeError):
        FlagType.FLOAT.coerce("k", "abc")


def test_string_and_json_coercion() -> None:
    assert FlagType.STRING.coerce("k", 5) == "5"
    assert FlagType.JSON.coerce("k", {"a": 1}) == {"a": 1}
    assert FlagType.JSON.coerce("k", [1, 2]) == [1, 2]
    with pytest.raises(FlagTypeError):
        FlagType.JSON.coerce("k", "not-json")


def test_none_is_always_coercible() -> None:
    for t in FlagType:
        assert t.coerce("k", None) is None


def test_spec_validates_default_eagerly() -> None:
    with pytest.raises(FlagTypeError):
        FlagSpec(key="x", type=FlagType.INT, default="not-an-int")


def test_spec_choices_enforced() -> None:
    spec = FlagSpec(
        key="video.backend",
        type=FlagType.STRING,
        default="dashscope",
        choices=("dashscope", "minimax"),
    )
    assert spec.coerce("minimax") == "minimax"
    with pytest.raises(FlagTypeError):
        spec.coerce("unknown-provider")


def test_choices_require_string_type() -> None:
    with pytest.raises(FlagTypeError):
        FlagSpec(key="x", type=FlagType.INT, default=1, choices=("a",))


def test_spec_empty_key_rejected() -> None:
    with pytest.raises(FlagTypeError):
        FlagSpec(key="", type=FlagType.BOOL, default=False)


def test_with_default_preserves_metadata() -> None:
    spec = FlagSpec(
        key="k",
        type=FlagType.BOOL,
        default=False,
        kill_switch=True,
        owner="ops",
        setting="kinora_live_video",
        tags=("a",),
    )
    rebound = spec.with_default(True)
    assert rebound.default is True
    assert rebound.kill_switch is True
    assert rebound.owner == "ops"
    assert rebound.setting == "kinora_live_video"
    assert rebound.tags == ("a",)


def test_to_dict_is_json_safe() -> None:
    spec = FlagSpec(key="k", type=FlagType.FLOAT, default=1.5, tags=("t",))
    d = spec.to_dict()
    assert d["key"] == "k"
    assert d["type"] == "float"
    assert d["default"] == 1.5
    assert d["tags"] == ["t"]
