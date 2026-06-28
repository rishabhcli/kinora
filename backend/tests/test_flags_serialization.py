"""Serialization roundtrip + audit-diff tests (pure)."""

from __future__ import annotations

import json

import pytest

from app.flags.audit import AuditAction, ChangeOp, diff, infer_action, summarize
from app.flags.errors import FlagValidationError
from app.flags.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricDirection,
    Variant,
)
from app.flags.models import (
    Clause,
    Flag,
    FlagKind,
    Operator,
    Prerequisite,
    Rollout,
    Rule,
    Target,
    Variation,
    WeightedVariation,
)
from app.flags.serialization import (
    experiment_from_dict,
    experiment_to_dict,
    flag_from_dict,
    flag_to_dict,
)


def _rich_flag() -> Flag:
    return Flag(
        key="render-ladder",
        kind=FlagKind.STRING,
        variations=(
            Variation("full", "full", name="Full video"),
            Variation("animatic", "animatic"),
            Variation("kenburns", "kenburns"),
        ),
        default_variation="kenburns",
        fallthrough=Rollout(
            (
                WeightedVariation("full", 2000),
                WeightedVariation("animatic", 3000),
                WeightedVariation("kenburns", 5000),
            ),
            bucket_by="session",
            seed=3,
        ),
        prerequisites=(Prerequisite("live-video", "on"),),
        targets=(Target("full", frozenset({"vip-1", "vip-2"})),),
        rules=(
            Rule(
                "fast-skimmer",
                (Clause("velocity_wps", Operator.GT, (8,)),),
                variation="kenburns",
            ),
            Rule(
                "eu",
                (Clause("region", Operator.IN, ("eu", "uk")),),
                rollout=Rollout(
                    (WeightedVariation("full", 5000), WeightedVariation("animatic", 5000))
                ),
            ),
        ),
        tags=("render", "perf"),
        name="Render ladder",
        description="degradation lane selector",
        version=4,
    )


def test_flag_roundtrip_is_lossless() -> None:
    flag = _rich_flag()
    data = flag_to_dict(flag)
    # JSON-safe
    json.dumps(data)
    restored = flag_from_dict(data)
    assert restored == flag


def test_boolean_flag_roundtrip() -> None:
    flag = Flag.boolean("live-video", enabled=False, rollout_percent=15.0)
    assert flag_from_dict(flag_to_dict(flag)) == flag


def test_flag_from_dict_rejects_missing_field() -> None:
    with pytest.raises(FlagValidationError):
        flag_from_dict({"key": "x"})  # no kind/variations/...


def test_flag_from_dict_rejects_bad_reference() -> None:
    bad = flag_to_dict(Flag.boolean("x"))
    bad["default_variation"] = "ghost"
    with pytest.raises(FlagValidationError):
        flag_from_dict(bad)


def test_experiment_roundtrip() -> None:
    exp = Experiment(
        key="crew-vs-baseline",
        variants=(
            Variant("baseline", 5000, is_control=True, flag_variation="single"),
            Variant("crew", 5000, flag_variation="crew"),
        ),
        salt="ccs-2026",
        status=ExperimentStatus.RUNNING,
        audience=(Rule("books", (Clause("genre", Operator.EQ, ("fable",)),), variation="x"),),
        traffic_percent=80.0,
        bucket_by="book",
        metrics=(
            Metric("ccs", name="character consistency"),
            Metric(
                "regen_rate",
                direction=MetricDirection.DECREASE,
                is_guardrail=True,
                guardrail_margin=0.05,
            ),
        ),
        version=2,
        name="Crew vs baseline",
    )
    data = experiment_to_dict(exp)
    json.dumps(data)
    assert experiment_from_dict(data) == exp


def test_experiment_from_dict_rejects_malformed() -> None:
    with pytest.raises(FlagValidationError):
        experiment_from_dict({"key": "x", "variants": [], "salt": "s"})


# --- audit diffing --------------------------------------------------------- #


def test_diff_detects_add_remove_replace() -> None:
    before = {"enabled": True, "rules": [{"id": "a"}], "name": "old"}
    after = {"enabled": False, "rules": [{"id": "a"}, {"id": "b"}], "extra": 1}
    changes = {c.path: c for c in diff(before, after)}
    assert changes["enabled"].op is ChangeOp.REPLACE
    assert changes["enabled"].before is True and changes["enabled"].after is False
    assert changes["name"].op is ChangeOp.REMOVE
    assert changes["extra"].op is ChangeOp.ADD
    assert changes["rules[1].id"].op is ChangeOp.ADD


def test_diff_create_and_delete() -> None:
    after = {"a": 1, "b": 2}
    assert all(c.op is ChangeOp.ADD for c in diff(None, after))
    assert all(c.op is ChangeOp.REMOVE for c in diff(after, None))


def test_infer_action() -> None:
    assert infer_action(None, {"x": 1}) is AuditAction.CREATE
    assert infer_action({"x": 1}, None) is AuditAction.DELETE
    assert (
        infer_action({"archived": False}, {"archived": True}) is AuditAction.ARCHIVE
    )
    assert (
        infer_action({"enabled": True, "v": 1}, {"enabled": False, "v": 1})
        is AuditAction.TOGGLE
    )
    assert (
        infer_action({"enabled": True, "name": "a"}, {"enabled": True, "name": "b"})
        is AuditAction.UPDATE
    )


def test_diff_is_stable_sorted() -> None:
    changes = diff({"z": 1, "a": 1}, {"z": 2, "a": 2})
    assert [c.path for c in changes] == ["a", "z"]


def test_summarize() -> None:
    changes = diff({"enabled": True}, {"enabled": False})
    assert "enabled" in summarize(changes)
    assert summarize([]) == "no changes"
    # truncation
    many = diff({}, {f"k{i}": i for i in range(20)})
    assert "more" in summarize(many, limit=3)


def test_real_flag_diff_is_readable() -> None:
    f1 = flag_to_dict(Flag.boolean("x", rollout_percent=10.0))
    f2 = flag_to_dict(Flag.boolean("x", rollout_percent=25.0))
    changes = diff(f1, f2)
    # the on/off weights changed
    paths = {c.path for c in changes}
    assert any("fallthrough.weights" in p for p in paths)
