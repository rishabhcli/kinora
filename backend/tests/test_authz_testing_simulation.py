"""Unit tests for the policy testing harness, coverage, and what-if simulation."""

from __future__ import annotations

from app.platform.authz.abac import AbacEffect, AbacRule, Attr, is_owner
from app.platform.authz.factory import build_plane
from app.platform.authz.model import Effect, Resource
from app.platform.authz.simulation import (
    Scenario,
    diff_planes,
    scenario_grid,
    would_change,
)
from app.platform.authz.testing import (
    PolicySuite,
    PolicyTestCase,
    coverage_report,
    declared_rules,
)


def _plane(rules=None):
    return build_plane(
        abac_rules=rules if rules is not None else (
            AbacRule(
                name="owner",
                actions=frozenset({"book:*"}),
                condition=is_owner(),
                effect=AbacEffect.ALLOW,
            ),
        ),
        include_auth_rbac=False,
    )


# -- policy testing ----------------------------------------------------------- #


def test_suite_pass_and_fail() -> None:
    plane = _plane()
    suite = PolicySuite([
        PolicyTestCase.allow(
            "owner-can-edit", "alice", "book:edit",
            Resource.of("book", "1", owner="alice"),
        ),
        PolicyTestCase.deny(
            "stranger-cannot-edit", "bob", "book:edit",
            Resource.of("book", "1", owner="alice"),
        ),
    ])
    result = suite.run(plane)
    assert result.passed
    assert result.total == 2
    assert not result.failures


def test_suite_detects_wrong_expectation() -> None:
    plane = _plane()
    suite = PolicySuite([
        PolicyTestCase.deny(  # wrong: owner IS allowed
            "owner-wrongly-expected-deny", "alice", "book:edit",
            Resource.of("book", "1", owner="alice"),
        ),
    ])
    result = suite.run(plane)
    assert not result.passed
    assert len(result.failures) == 1
    assert result.failures[0].actual is Effect.ALLOW


def test_suite_expect_rule_assertion() -> None:
    plane = _plane()
    ok = PolicySuite([
        PolicyTestCase.allow(
            "owner-via-rule", "alice", "book:edit",
            Resource.of("book", "1", owner="alice"),
            expect_rule="owner",
        ),
    ]).run(plane)
    assert ok.passed
    bad = PolicySuite([
        PolicyTestCase.allow(
            "owner-wrong-rule", "alice", "book:edit",
            Resource.of("book", "1", owner="alice"),
            expect_rule="nonexistent-rule",
        ),
    ]).run(plane)
    assert not bad.passed  # the named rule did not fire


# -- coverage ----------------------------------------------------------------- #


def test_declared_rules_includes_abac_names() -> None:
    plane = _plane()
    declared = declared_rules(plane)
    assert "owner" in declared


def test_coverage_report_flags_uncovered() -> None:
    rules = (
        AbacRule(name="owner", actions=frozenset({"book:*"}), condition=is_owner()),
        AbacRule(
            name="never-fires",
            actions=frozenset({"book:edit"}),
            condition=Attr("subject.id", "eq", "impossible-user"),
        ),
    )
    plane = _plane(rules)
    suite = PolicySuite([
        PolicyTestCase.allow(
            "owner", "alice", "book:edit", Resource.of("book", "1", owner="alice")
        ),
    ])
    report = coverage_report(plane, suite)
    assert "owner" in report.fired
    assert "never-fires" in report.uncovered
    assert report.ratio < 1.0


def test_coverage_full() -> None:
    plane = _plane()
    suite = PolicySuite([
        PolicyTestCase.allow(
            "owner", "alice", "book:edit", Resource.of("book", "1", owner="alice")
        ),
    ])
    report = coverage_report(plane, suite)
    assert report.ratio == 1.0
    assert not report.uncovered


# -- simulation --------------------------------------------------------------- #


def test_diff_planes_detects_newly_denied() -> None:
    # current: owner allowed. candidate: add a deny-all rule → owner now denied.
    current = _plane()
    candidate_rules = (
        AbacRule(
            name="emergency-lockdown",
            actions=frozenset({"*"}),
            condition=Attr("action", "ne", "nonsense"),  # always true
            effect=AbacEffect.DENY,
        ),
        AbacRule(name="owner", actions=frozenset({"book:*"}), condition=is_owner()),
    )
    candidate = _plane(candidate_rules)
    scenarios = [
        Scenario("alice", "book:edit", Resource.of("book", "1", owner="alice")),
    ]
    result = diff_planes(current, candidate, scenarios)
    assert len(result.flipped) == 1
    assert result.flipped[0].newly_denied
    assert len(result.newly_denied) == 1


def test_diff_planes_detects_newly_allowed() -> None:
    current = _plane()
    # candidate adds an allow rule for everyone on book:view
    candidate_rules = (
        AbacRule(name="owner", actions=frozenset({"book:*"}), condition=is_owner()),
        AbacRule(
            name="public-view",
            actions=frozenset({"book:view"}),
            condition=Attr("resource.public", "eq", True),
            effect=AbacEffect.ALLOW,
        ),
    )
    candidate = _plane(candidate_rules)
    scenarios = [
        Scenario("bob", "book:view", Resource.of("book", "1", owner="alice", public=True)),
    ]
    result = diff_planes(current, candidate, scenarios)
    assert len(result.newly_allowed) == 1


def test_would_change_single_question() -> None:
    current = _plane()
    candidate = _plane((
        AbacRule(
            name="deny-all",
            actions=frozenset({"*"}),
            condition=Attr("action", "ne", "x"),
            effect=AbacEffect.DENY,
        ),
    ))
    flip = would_change(
        current, candidate,
        Scenario("alice", "book:edit", Resource.of("book", "1", owner="alice")),
    )
    assert flip is not None and flip.newly_denied
    # an unchanged scenario returns None
    same = would_change(
        current, current,
        Scenario("alice", "book:edit", Resource.of("book", "1", owner="alice")),
    )
    assert same is None


def test_scenario_grid_cartesian_product() -> None:
    grid = scenario_grid(
        subjects=["alice", "bob"],
        actions=["book:view", "book:edit"],
        resources=[Resource.of("book", "1"), Resource.of("book", "2")],
    )
    assert len(grid) == 2 * 2 * 2
    labels = {s.label for s in grid}
    assert "user:alice book:view book:1" in labels


def test_simulation_render_no_crash() -> None:
    current = _plane()
    candidate = _plane()
    result = diff_planes(
        current, candidate,
        [Scenario("alice", "book:edit", Resource.of("book", "1", owner="alice"))],
    )
    assert "simulation:" in result.render()
    assert result.unchanged == 1
