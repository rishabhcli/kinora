"""Targeting predicate tests — every operator, plus type-mismatch safety."""

from __future__ import annotations

from app.flags.context import EvalContext
from app.flags.models import Clause, Operator, Rule
from app.flags.targeting import clause_matches, rule_matches


def ctx(**attrs: object) -> EvalContext:
    return EvalContext.of("u", **attrs)  # type: ignore[arg-type]


def test_eq_and_neq() -> None:
    assert clause_matches(Clause("plan", Operator.EQ, ("pro",)), ctx(plan="pro"))
    assert not clause_matches(Clause("plan", Operator.EQ, ("pro",)), ctx(plan="free"))
    assert clause_matches(Clause("plan", Operator.NEQ, ("pro",)), ctx(plan="free"))


def test_in_and_not_in() -> None:
    c = Clause("country", Operator.IN, ("US", "CA", "MX"))
    assert clause_matches(c, ctx(country="CA"))
    assert not clause_matches(c, ctx(country="FR"))
    assert clause_matches(Clause("country", Operator.NOT_IN, ("US",)), ctx(country="FR"))


def test_in_with_list_attribute() -> None:
    c = Clause("groups", Operator.IN, ("beta",))
    assert clause_matches(c, ctx(groups=["alpha", "beta"]))
    assert not clause_matches(c, ctx(groups=["alpha"]))


def test_contains_string_and_list() -> None:
    assert clause_matches(Clause("email", Operator.CONTAINS, ("@corp",)), ctx(email="a@corp.com"))
    assert clause_matches(Clause("tags", Operator.CONTAINS, ("vip",)), ctx(tags=["vip", "x"]))
    assert clause_matches(
        Clause("tags", Operator.NOT_CONTAINS, ("vip",)), ctx(tags=["plain"])
    )


def test_starts_and_ends_with() -> None:
    assert clause_matches(Clause("v", Operator.STARTS_WITH, ("2.",)), ctx(v="2.4.1"))
    assert clause_matches(Clause("v", Operator.ENDS_WITH, (".1",)), ctx(v="2.4.1"))
    assert not clause_matches(Clause("v", Operator.STARTS_WITH, ("3.",)), ctx(v="2.4.1"))


def test_regex_matches_and_invalid_pattern_is_safe() -> None:
    assert clause_matches(Clause("e", Operator.MATCHES, (r".+@gmail\.com$",)), ctx(e="x@gmail.com"))
    # An invalid regex never matches (no raise).
    assert not clause_matches(Clause("e", Operator.MATCHES, ("(",)), ctx(e="x"))
    # Non-string actual never matches a regex.
    assert not clause_matches(Clause("n", Operator.MATCHES, (r"\d",)), ctx(n=5))


def test_numeric_operators() -> None:
    assert clause_matches(Clause("age", Operator.GT, (18,)), ctx(age=21))
    assert clause_matches(Clause("age", Operator.GTE, (21,)), ctx(age=21))
    assert clause_matches(Clause("age", Operator.LT, (30,)), ctx(age=21))
    assert clause_matches(Clause("age", Operator.LTE, (21,)), ctx(age=21))
    assert not clause_matches(Clause("age", Operator.GT, (30,)), ctx(age=21))


def test_numeric_coerces_string_attributes() -> None:
    assert clause_matches(Clause("n", Operator.GT, (5,)), ctx(n="10"))
    # non-numeric string is a safe non-match
    assert not clause_matches(Clause("n", Operator.GT, (5,)), ctx(n="abc"))


def test_numeric_rejects_bool() -> None:
    # bool is not treated as numeric (True != 1 here)
    assert not clause_matches(Clause("flag", Operator.GT, (0,)), ctx(flag=True))


def test_semver_operators() -> None:
    assert clause_matches(Clause("app", Operator.SEMVER_GTE, ("2.4.0",)), ctx(app="2.4.1"))
    assert clause_matches(Clause("app", Operator.SEMVER_GT, ("2.3.9",)), ctx(app="2.4.0"))
    assert clause_matches(Clause("app", Operator.SEMVER_LT, ("3.0.0",)), ctx(app="2.9.9"))
    assert clause_matches(Clause("app", Operator.SEMVER_EQ, ("v1.0.0",)), ctx(app="1.0.0"))
    assert not clause_matches(Clause("app", Operator.SEMVER_GTE, ("2.5.0",)), ctx(app="2.4.1"))


def test_semver_two_component_and_prerelease() -> None:
    assert clause_matches(Clause("app", Operator.SEMVER_GTE, ("2.4",)), ctx(app="2.4.0"))
    assert clause_matches(Clause("app", Operator.SEMVER_LT, ("2.5.0",)), ctx(app="2.4.1-beta"))


def test_semver_invalid_is_safe() -> None:
    assert not clause_matches(Clause("app", Operator.SEMVER_GT, ("1.0.0",)), ctx(app="garbage"))


def test_exists_and_not_exists() -> None:
    assert clause_matches(Clause("beta", Operator.EXISTS), ctx(beta=True))
    assert clause_matches(Clause("beta", Operator.NOT_EXISTS), ctx())
    assert not clause_matches(Clause("beta", Operator.EXISTS), ctx())


def test_percentage_operator_buckets() -> None:
    # 0..5000bp == first half of the population for this attribute's key bucketing.
    c = Clause("x", Operator.PERCENTAGE, (0, 5000))
    hits = sum(1 for i in range(5000) if clause_matches(c, EvalContext.of(f"u{i}")))
    assert 0.45 < hits / 5000 < 0.55


def test_negate_inverts() -> None:
    base = Clause("plan", Operator.EQ, ("pro",))
    neg = Clause("plan", Operator.EQ, ("pro",), negate=True)
    assert clause_matches(base, ctx(plan="pro"))
    assert not clause_matches(neg, ctx(plan="pro"))
    assert clause_matches(neg, ctx(plan="free"))


def test_rule_is_and_of_clauses() -> None:
    rule = Rule(
        "eu-pro",
        (
            Clause("region", Operator.EQ, ("eu",)),
            Clause("plan", Operator.EQ, ("pro",)),
        ),
        variation="a",
    )
    assert rule_matches(rule, ctx(region="eu", plan="pro"))
    assert not rule_matches(rule, ctx(region="eu", plan="free"))


def test_empty_rule_matches_everyone() -> None:
    assert rule_matches(Rule("all", (), variation="a"), ctx())


def test_missing_attribute_is_safe_non_match() -> None:
    assert not clause_matches(Clause("absent", Operator.EQ, ("x",)), ctx())
    assert not clause_matches(Clause("absent", Operator.GT, (1,)), ctx())
