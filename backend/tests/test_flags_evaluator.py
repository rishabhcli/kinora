"""Evaluator waterfall tests — the total, never-raising decision path."""

from __future__ import annotations

from app.flags.context import EvalContext
from app.flags.evaluator import FlagEvaluator
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


def ev(*flags: Flag, version: int = 1, salt: str = "") -> FlagEvaluator:
    return FlagEvaluator(FlagSnapshot.from_flags(flags, version=version), default_salt=salt)


def test_flag_not_found_returns_default() -> None:
    e = ev()
    r = e.evaluate("nope", EvalContext.of("u"), default="fallback")
    assert r.value == "fallback"
    assert r.reason is Reason.FLAG_NOT_FOUND
    assert r.is_default


def test_archived_flag_serves_default_variation() -> None:
    f = Flag.boolean("x", enabled=True, default=False, archived=True)
    r = ev(f).evaluate("x", EvalContext.of("u"))
    assert r.reason is Reason.FLAG_ARCHIVED
    assert r.value is False


def test_disabled_flag_serves_default_variation() -> None:
    f = Flag.boolean("x", enabled=False, default=False)
    r = ev(f).evaluate("x", EvalContext.of("u"))
    assert r.reason is Reason.FLAG_OFF
    assert r.value is False


def test_individual_target_wins_over_rules() -> None:
    f = Flag(
        key="x",
        kind=FlagKind.STRING,
        variations=(Variation("a", "a"), Variation("b", "b"), Variation("c", "c")),
        default_variation="a",
        fallthrough=Rollout.single("a"),
        targets=(Target("c", frozenset({"vip"})),),
        rules=(Rule("r", (Clause("plan", Operator.EQ, ("pro",)),), variation="b"),),
    )
    e = ev(f)
    # vip is pinned to c even though they'd match the pro rule
    r = e.evaluate("x", EvalContext.of("vip", plan="pro"))
    assert r.value == "c"
    assert r.reason is Reason.TARGET_MATCH
    # a non-targeted pro user matches the rule
    r2 = e.evaluate("x", EvalContext.of("u2", plan="pro"))
    assert r2.value == "b"
    assert r2.reason is Reason.RULE_MATCH
    assert r2.rule_id == "r"


def test_first_matching_rule_wins() -> None:
    f = Flag(
        key="x",
        kind=FlagKind.STRING,
        variations=(Variation("a", "a"), Variation("b", "b")),
        default_variation="a",
        fallthrough=Rollout.single("a"),
        rules=(
            Rule("r1", (Clause("region", Operator.EQ, ("eu",)),), variation="a"),
            Rule("r2", (Clause("plan", Operator.EQ, ("pro",)),), variation="b"),
        ),
    )
    # matches both rules; first one (r1) wins
    r = ev(f).evaluate("x", EvalContext.of("u", region="eu", plan="pro"))
    assert r.rule_id == "r1"
    assert r.value == "a"


def test_fallthrough_when_no_rule_matches() -> None:
    f = Flag(
        key="x",
        kind=FlagKind.STRING,
        variations=(Variation("a", "a"), Variation("b", "b")),
        default_variation="a",
        fallthrough=Rollout(
            (WeightedVariation("a", 5000), WeightedVariation("b", 5000))
        ),
        rules=(Rule("r", (Clause("plan", Operator.EQ, ("pro",)),), variation="a"),),
    )
    e = ev(f)
    r = e.evaluate("x", EvalContext.of("u", plan="free"))
    assert r.reason is Reason.FALLTHROUGH
    assert r.value in ("a", "b")


def test_fallthrough_split_is_proportional() -> None:
    f = Flag(
        key="x",
        kind=FlagKind.STRING,
        variations=(Variation("a", "a"), Variation("b", "b")),
        default_variation="a",
        fallthrough=Rollout(
            (WeightedVariation("a", 7000), WeightedVariation("b", 3000))
        ),
    )
    e = ev(f)
    bs = sum(
        1 for i in range(8000) if e.evaluate("x", EvalContext.of(f"u{i}")).value == "b"
    )
    assert 0.27 < bs / 8000 < 0.33


def test_rule_rollout_within_rule() -> None:
    f = Flag(
        key="x",
        kind=FlagKind.STRING,
        variations=(Variation("a", "a"), Variation("b", "b")),
        default_variation="a",
        fallthrough=Rollout.single("a"),
        rules=(
            Rule(
                "eu",
                (Clause("region", Operator.EQ, ("eu",)),),
                rollout=Rollout((WeightedVariation("a", 5000), WeightedVariation("b", 5000))),
            ),
        ),
    )
    e = ev(f)
    bs = sum(
        1
        for i in range(6000)
        if e.evaluate("x", EvalContext.of(f"u{i}", region="eu")).value == "b"
    )
    assert 0.45 < bs / 6000 < 0.55


def test_prerequisite_pass_and_fail() -> None:
    dep = Flag.boolean("dep", enabled=True, default=True)  # serves "on"
    gated = Flag(
        key="gated",
        kind=FlagKind.BOOLEAN,
        variations=(Variation("on", True), Variation("off", False)),
        default_variation="off",
        fallthrough=Rollout.single("on"),
        prerequisites=(Prerequisite("dep", "on"),),
    )
    e = ev(dep, gated)
    assert e.evaluate("gated", EvalContext.of("u")).value is True

    # flip the prerequisite to serve "off"
    dep_off = Flag.boolean("dep", enabled=False)  # OFF -> default "off"
    e2 = ev(dep_off, gated)
    r = e2.evaluate("gated", EvalContext.of("u"))
    assert r.reason is Reason.PREREQUISITE_FAILED
    assert r.value is False


def test_missing_prerequisite_fails_safe() -> None:
    gated = Flag(
        key="gated",
        kind=FlagKind.BOOLEAN,
        variations=(Variation("on", True), Variation("off", False)),
        default_variation="off",
        fallthrough=Rollout.single("on"),
        prerequisites=(Prerequisite("ghost", "on"),),
    )
    r = ev(gated).evaluate("gated", EvalContext.of("u"))
    assert r.reason is Reason.PREREQUISITE_FAILED


def test_cyclic_prerequisites_do_not_recurse_forever() -> None:
    a = Flag(
        key="a",
        kind=FlagKind.BOOLEAN,
        variations=(Variation("on", True), Variation("off", False)),
        default_variation="off",
        fallthrough=Rollout.single("on"),
        prerequisites=(Prerequisite("b", "on"),),
    )
    b = Flag(
        key="b",
        kind=FlagKind.BOOLEAN,
        variations=(Variation("on", True), Variation("off", False)),
        default_variation="off",
        fallthrough=Rollout.single("on"),
        prerequisites=(Prerequisite("a", "on"),),
    )
    # Must terminate (cycle treated as failed prereq), not blow the stack.
    r = ev(a, b).evaluate("a", EvalContext.of("u"))
    assert r.reason is Reason.PREREQUISITE_FAILED


def test_evaluation_is_sticky_per_unit() -> None:
    f = Flag.boolean("x", rollout_percent=40.0)
    e = ev(f)
    for i in range(200):
        u = EvalContext.of(f"u{i}")
        first = e.evaluate("x", u).value
        again = e.evaluate("x", u).value
        assert first == again


def test_bucket_diagnostic() -> None:
    f = Flag.boolean("x", rollout_percent=50.0)
    e = ev(f)
    assert 0 <= (e.bucket("x", EvalContext.of("u")) or -1) < 10_000
    assert e.bucket("ghost", EvalContext.of("u")) is None


def test_salt_namespacing_decorrelates_flags() -> None:
    # Two identical 50% boolean flags should not assign the same users on/off in
    # lockstep — the per-flag salt decorrelates them.
    f1 = Flag.boolean("flag-one", rollout_percent=50.0)
    f2 = Flag.boolean("flag-two", rollout_percent=50.0)
    e = ev(f1, f2, salt="kinora")
    agree = sum(
        1
        for i in range(3000)
        if e.evaluate("flag-one", EvalContext.of(f"u{i}")).value
        == e.evaluate("flag-two", EvalContext.of(f"u{i}")).value
    )
    # If correlated they'd agree ~100%; decorrelated they agree ~50%.
    assert 0.45 < agree / 3000 < 0.55
