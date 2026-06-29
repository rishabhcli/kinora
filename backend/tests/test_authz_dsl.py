"""Unit tests for the Rego-style policy DSL: parse, evaluate, partial-eval."""

from __future__ import annotations

import pytest

from app.platform.authz.dsl import (
    PolicyEngine,
    PolicyParseError,
    Truth,
    evaluate_policy,
    parse_policy,
    partial_evaluate,
)
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Effect,
    Resource,
    Subject,
)

OWNER_POLICY = """
package book.access
default allow = false

allow if {
    input.action == "book:read"
    input.subject.id == input.resource.owner
}

allow if {
    input.subject.is_admin == true
}

deny if {
    input.context.suspended == true
}
"""


def _req(action, subject_attrs=None, resource_attrs=None, ctx=None):
    return AuthorizationRequest(
        subject=Subject(type="user", id="alice", attributes=subject_attrs or {}),
        action=action,
        resource=Resource(type="book", id="42", attributes=resource_attrs or {}),
        context=Context(attributes=ctx or {}),
    )


# -- parsing ----------------------------------------------------------------- #


def test_parse_package_default_and_rules() -> None:
    policy = parse_policy(OWNER_POLICY)
    assert policy.package == "book.access"
    assert policy.default_allow is False
    assert len(policy.allow_rules()) == 1  # two `allow if` blocks merge into one rule
    assert len(policy.allow_rules()[0].bodies) == 2
    assert len(policy.deny_rules()) == 1


def test_parse_literals() -> None:
    policy = parse_policy(
        """
        allow if {
            input.context.n == 5
            input.context.f == 1.5
            input.context.flag == true
            input.context.name == "bob"
            input.context.nothing == null
        }
        """
    )
    body = policy.allow_rules()[0].bodies[0]
    rights = [e.right for e in body]
    assert rights == [5, 1.5, True, "bob", None]


def test_parse_rejects_non_input_left() -> None:
    with pytest.raises(PolicyParseError):
        parse_policy("allow if {\n  5 == input.x\n}")


def test_parse_rejects_unterminated_body() -> None:
    with pytest.raises(PolicyParseError):
        parse_policy("allow if {\n  input.action == \"x\"")


def test_parse_rejects_non_rule() -> None:
    with pytest.raises(PolicyParseError):
        parse_policy("garbage line here")


# -- full evaluation --------------------------------------------------------- #


def test_evaluate_owner_allowed() -> None:
    policy = parse_policy(OWNER_POLICY)
    allow, reasons = evaluate_policy(
        policy, _req("book:read", {"id": "alice"}, {"owner": "alice"})
    )
    assert allow
    assert any("allow body fired" in r for r in reasons)


def test_evaluate_non_owner_denied_by_default() -> None:
    policy = parse_policy(OWNER_POLICY)
    allow, _ = evaluate_policy(policy, _req("book:read", {"id": "alice"}, {"owner": "bob"}))
    assert not allow


def test_evaluate_admin_allowed() -> None:
    policy = parse_policy(OWNER_POLICY)
    allow, _ = evaluate_policy(policy, _req("book:read", {"is_admin": True}))
    assert allow


def test_evaluate_deny_overrides_allow() -> None:
    policy = parse_policy(OWNER_POLICY)
    # owner would be allowed, but suspended context fires the deny rule
    allow, reasons = evaluate_policy(
        policy,
        _req("book:read", {"id": "alice"}, {"owner": "alice"}, {"suspended": True}),
    )
    assert not allow
    assert any("deny body fired" in r for r in reasons)


def test_evaluate_default_allow() -> None:
    policy = parse_policy("default allow = true\n")
    allow, reasons = evaluate_policy(policy, _req("anything:goes"))
    assert allow
    assert "default allow" in reasons


def test_evaluate_relational_and_in_operators() -> None:
    policy = parse_policy(
        """
        allow if {
            input.context.speed <= 10
            input.action in input.subject.allowed
        }
        """
    )
    req = _req(
        "book:read",
        subject_attrs={"allowed": ["book:read", "book:write"]},
        ctx={"speed": 5},
    )
    allow, _ = evaluate_policy(policy, req)
    assert allow


# -- partial evaluation ------------------------------------------------------ #


def test_partial_decided_when_deny_fully_true() -> None:
    policy = parse_policy(OWNER_POLICY)
    res = partial_evaluate(policy, {"context": {"suspended": True}})
    assert res.decided and res.allow is False


def test_partial_decided_when_allow_fully_true() -> None:
    policy = parse_policy(OWNER_POLICY)
    # admin allow body is fully satisfied; no deny known to fire
    res = partial_evaluate(
        policy, {"subject": {"is_admin": True}, "context": {"suspended": False}}
    )
    assert res.decided and res.allow is True


def test_partial_residual_when_resource_unknown() -> None:
    policy = parse_policy(OWNER_POLICY)
    # subject + action known, resource.owner unknown → undecided with residual
    res = partial_evaluate(
        policy,
        {"action": "book:read", "subject": {"id": "alice", "is_admin": False}},
    )
    assert not res.decided
    # the residual allow body should be the still-unknown owner comparison
    flat = [e.render() for body in res.residual_allow for e in body]
    assert any("resource.owner" in r for r in flat)


def test_partial_decided_deny_when_nothing_can_grant() -> None:
    # default-deny policy where the only allow body is already false
    policy = parse_policy(
        """
        default allow = false
        allow if {
            input.subject.id == "root"
        }
        """
    )
    res = partial_evaluate(policy, {"subject": {"id": "alice"}})
    assert res.decided and res.allow is False


def test_truth_enum_values() -> None:
    assert Truth.TRUE.value == "true"
    assert Truth.UNKNOWN.value == "unknown"


# -- the DSL engine ---------------------------------------------------------- #


def test_policy_engine_allow_and_deny() -> None:
    engine = PolicyEngine.from_sources(OWNER_POLICY)
    allow = engine.evaluate(_req("book:read", {"id": "alice"}, {"owner": "alice"}))
    assert allow.effect is Effect.ALLOW
    deny = engine.evaluate(
        _req("book:read", {"id": "alice"}, {"owner": "alice"}, {"suspended": True})
    )
    assert deny.effect is Effect.DENY


def test_policy_engine_abstains_when_nothing_fires() -> None:
    engine = PolicyEngine.from_sources(OWNER_POLICY)
    res = engine.evaluate(_req("book:read", {"id": "alice"}, {"owner": "bob"}))
    assert res.effect is Effect.ABSTAIN
