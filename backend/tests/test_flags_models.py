"""Model validation tests — the flag/rollout invariants enforced at build time."""

from __future__ import annotations

import pytest

from app.flags.errors import FlagValidationError
from app.flags.models import (
    Clause,
    Flag,
    FlagKind,
    FlagSnapshot,
    Operator,
    Prerequisite,
    Reason,
    Rollout,
    Rule,
    Target,
    Variation,
    WeightedVariation,
)


def test_variation_requires_key() -> None:
    with pytest.raises(FlagValidationError):
        Variation("", True)


def test_clause_requires_values_for_binary_ops() -> None:
    with pytest.raises(FlagValidationError):
        Clause("plan", Operator.EQ, ())
    # unary ops don't need values
    Clause("plan", Operator.EXISTS)
    Clause("plan", Operator.NOT_EXISTS)


def test_rollout_weights_must_sum_to_total() -> None:
    with pytest.raises(FlagValidationError):
        Rollout((WeightedVariation("a", 5000), WeightedVariation("b", 4000)))
    # exactly 10000 is fine
    Rollout((WeightedVariation("a", 5000), WeightedVariation("b", 5000)))


def test_rollout_single_and_even() -> None:
    assert Rollout.single("on").variation_keys() == ("on",)
    even = Rollout.even(("a", "b", "c"))
    assert sum(w.weight for w in even.weights) == 10_000
    assert even.variation_keys() == ("a", "b", "c")


def test_rule_exactly_one_outcome() -> None:
    with pytest.raises(FlagValidationError):
        Rule("r", (Clause("x", Operator.EXISTS),))  # neither
    with pytest.raises(FlagValidationError):
        Rule(
            "r",
            (Clause("x", Operator.EXISTS),),
            variation="a",
            rollout=Rollout.single("a"),
        )  # both


def test_flag_default_must_be_known() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.STRING,
            variations=(Variation("a", "a"),),
            default_variation="missing",
            fallthrough=Rollout.single("a"),
        )


def test_flag_duplicate_variation_keys_rejected() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.STRING,
            variations=(Variation("a", "1"), Variation("a", "2")),
            default_variation="a",
            fallthrough=Rollout.single("a"),
        )


def test_flag_rule_references_validated() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.STRING,
            variations=(Variation("a", "a"),),
            default_variation="a",
            fallthrough=Rollout.single("a"),
            rules=(Rule("r", (Clause("x", Operator.EXISTS),), variation="ghost"),),
        )


def test_flag_target_references_validated() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.STRING,
            variations=(Variation("a", "a"),),
            default_variation="a",
            fallthrough=Rollout.single("a"),
            targets=(Target("ghost", frozenset({"u1"})),),
        )


def test_flag_fallthrough_references_validated() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.STRING,
            variations=(Variation("a", "a"),),
            default_variation="a",
            fallthrough=Rollout.single("ghost"),
        )


def test_boolean_flag_rejects_non_bool_variation_value() -> None:
    with pytest.raises(FlagValidationError):
        Flag(
            key="f",
            kind=FlagKind.BOOLEAN,
            variations=(Variation("on", "yes"), Variation("off", False)),
            default_variation="off",
            fallthrough=Rollout.single("on"),
        )


def test_boolean_constructor() -> None:
    f = Flag.boolean("x", enabled=True, default=True)
    assert f.kind is FlagKind.BOOLEAN
    assert f.default_variation == "on"
    assert {v.key for v in f.variations} == {"on", "off"}


def test_boolean_constructor_with_rollout_percent() -> None:
    f = Flag.boolean("x", rollout_percent=25.0)
    weights = {w.variation: w.weight for w in f.fallthrough.weights}
    assert weights == {"on": 2500, "off": 7500}


def test_multivariate_constructor_even_default() -> None:
    f = Flag.multivariate(
        "mv",
        (Variation("a", "a"), Variation("b", "b"), Variation("c", "c")),
        default="a",
    )
    assert sum(w.weight for w in f.fallthrough.weights) == 10_000


def test_prerequisite_validation() -> None:
    with pytest.raises(FlagValidationError):
        Prerequisite("", "on")
    Prerequisite("dep", "on")


def test_variation_lookups() -> None:
    f = Flag.multivariate(
        "mv", (Variation("a", 1), Variation("b", 2)), default="a", kind=FlagKind.NUMBER
    )
    assert f.variation_by_key("b").value == 2
    assert f.variation_index("b") == 1
    with pytest.raises(FlagValidationError):
        f.variation_by_key("zzz")


def test_snapshot_rejects_duplicate_flags() -> None:
    f = Flag.boolean("dup")
    with pytest.raises(FlagValidationError):
        FlagSnapshot.from_flags((f, f))


def test_snapshot_with_and_without_flag_bumps_version() -> None:
    snap = FlagSnapshot.from_flags((Flag.boolean("a"),), version=3)
    snap2 = snap.with_flag(Flag.boolean("b"))
    assert snap2.version == 4
    assert set(snap2.keys()) == {"a", "b"}
    snap3 = snap2.without_flag("a")
    assert snap3.version == 5
    assert snap3.keys() == ("b",)


def test_evaluation_to_dict_roundtrips_reason() -> None:
    from app.flags.models import Evaluation

    ev = Evaluation("f", True, "on", 0, Reason.RULE_MATCH, flag_version=2, rule_id="r1")
    d = ev.to_dict()
    assert d["reason"] == "rule_match"
    assert d["rule_id"] == "r1"
    assert d["value"] is True
