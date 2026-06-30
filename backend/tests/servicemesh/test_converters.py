"""ConverterRegistry: adjacent migrators composed into up/down chains, no-path."""

from __future__ import annotations

import pytest

from app.servicemesh.converters import ConverterRegistry, Direction, Payload
from app.servicemesh.errors import ConversionError, NoConversionPathError
from app.servicemesh.versioning import SemVer


def _build_chain() -> ConverterRegistry:
    """A v1<->v2<->v3 chain for schema 'shot.render.job'.

    v1: {shot_hash, prio}      v2: {shot_hash, priority}   (rename prio->priority)
    v2: {..., budget}          v3: {..., budget_seconds}   (rename budget->budget_seconds)
    """
    reg = ConverterRegistry()

    def up_1_2(p: Payload) -> Payload:
        p["priority"] = p.pop("prio", 0)
        return p

    def down_2_1(p: Payload) -> Payload:
        p["prio"] = p.pop("priority", 0)
        return p

    def up_2_3(p: Payload) -> Payload:
        p["budget_seconds"] = p.pop("budget", 0.0)
        return p

    def down_3_2(p: Payload) -> Payload:
        p["budget"] = p.pop("budget_seconds", 0.0)
        return p

    reg.register_pair("shot.render.job", "1.0.0", "2.0.0", up=up_1_2, down=down_2_1)
    reg.register_pair("shot.render.job", "2.0.0", "3.0.0", up=up_2_3, down=down_3_2)
    return reg


def test_single_step_upgrade() -> None:
    reg = _build_chain()
    out = reg.convert("shot.render.job", {"shot_hash": "h", "prio": 5}, "1.0.0", "2.0.0")
    assert out == {"shot_hash": "h", "priority": 5}


def test_multi_hop_upgrade_v1_to_v3() -> None:
    reg = _build_chain()
    out = reg.convert(
        "shot.render.job", {"shot_hash": "h", "prio": 5, "budget": 12.5}, "1.0.0", "3.0.0"
    )
    assert out == {"shot_hash": "h", "priority": 5, "budget_seconds": 12.5}


def test_multi_hop_downgrade_v3_to_v1() -> None:
    reg = _build_chain()
    out = reg.convert(
        "shot.render.job",
        {"shot_hash": "h", "priority": 5, "budget_seconds": 12.5},
        "3.0.0",
        "1.0.0",
    )
    assert out == {"shot_hash": "h", "prio": 5, "budget": 12.5}


def test_identity_conversion_is_a_copy() -> None:
    reg = _build_chain()
    src = {"shot_hash": "h"}
    out = reg.convert("shot.render.job", src, "2.0.0", "2.0.0")
    assert out == src
    assert out is not src  # never returns the caller's object


def test_does_not_mutate_caller_payload() -> None:
    reg = _build_chain()
    src = {"shot_hash": "h", "prio": 5}
    reg.convert("shot.render.job", src, "1.0.0", "2.0.0")
    assert src == {"shot_hash": "h", "prio": 5}  # untouched


def test_plan_finds_shortest_path() -> None:
    reg = _build_chain()
    plan = reg.plan("shot.render.job", "1.0.0", "3.0.0")
    assert plan.hops == 2
    assert plan.steps[0].direction is Direction.UP
    assert plan.steps[0].from_version == SemVer.parse("1.0.0")
    assert plan.steps[1].to_version == SemVer.parse("3.0.0")


def test_no_path_raises() -> None:
    reg = _build_chain()
    with pytest.raises(NoConversionPathError):
        reg.plan("shot.render.job", "1.0.0", "9.0.0")
    with pytest.raises(NoConversionPathError):
        reg.convert("shot.render.job", {}, "1.0.0", "9.0.0")
    assert not reg.can_convert("shot.render.job", "1.0.0", "9.0.0")
    assert reg.can_convert("shot.render.job", "1.0.0", "3.0.0")


def test_unknown_schema_has_no_path() -> None:
    reg = _build_chain()
    assert not reg.has_any("never.seen")
    assert reg.has_any("shot.render.job")
    with pytest.raises(NoConversionPathError):
        reg.plan("never.seen", "1.0.0", "2.0.0")


def test_migrator_direction_inferred() -> None:
    reg = ConverterRegistry()
    up = reg.register("s", "1.0.0", "2.0.0", lambda p: p)
    down = reg.register("s", "2.0.0", "1.0.0", lambda p: p)
    assert up.direction is Direction.UP
    assert down.direction is Direction.DOWN


def test_failing_migrator_wrapped_as_conversion_error() -> None:
    reg = ConverterRegistry()

    def boom(_p: Payload) -> Payload:
        raise KeyError("missing")

    reg.register("s", "1.0.0", "2.0.0", boom)
    with pytest.raises(ConversionError):
        reg.convert("s", {}, "1.0.0", "2.0.0")
