"""Authorization-policy (which-workload-may-call-which) tests."""

from __future__ import annotations

import pytest

from app.zerotrust.identity import (
    AuthorizationError,
    AuthorizationPolicy,
    CallRequest,
    PolicyRule,
    SpiffeId,
    matcher_for,
)
from app.zerotrust.identity.policy import (
    AnyWorkload,
    DomainMember,
    ExactId,
    PathPrefix,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def _req(caller: str, target: str, action: str = "call") -> CallRequest:
    return CallRequest(SpiffeId.parse(caller), SpiffeId.parse(target), action)


def test_default_deny() -> None:
    pol = AuthorizationPolicy()
    d = pol.evaluate(_req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b"))
    assert not d.allowed
    assert "default-deny" in d.reason


def test_exact_allow() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r", callers=[f"spiffe://{TRUST_DOMAIN}/a"], targets=[f"spiffe://{TRUST_DOMAIN}/b"]
        )
    )
    assert pol.is_allowed(_req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b"))
    assert not pol.is_allowed(_req(f"spiffe://{TRUST_DOMAIN}/c", f"spiffe://{TRUST_DOMAIN}/b"))


def test_path_prefix_match() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r",
            callers=[f"spiffe://{TRUST_DOMAIN}/agents/*"],
            targets=[f"spiffe://{TRUST_DOMAIN}/mcp"],
        )
    )
    assert pol.is_allowed(
        _req(f"spiffe://{TRUST_DOMAIN}/agents/critic", f"spiffe://{TRUST_DOMAIN}/mcp")
    )
    # prefix confusion must not match
    assert not pol.is_allowed(
        _req(f"spiffe://{TRUST_DOMAIN}/agents-evil", f"spiffe://{TRUST_DOMAIN}/mcp")
    )


def test_deny_overrides_allow() -> None:
    pol = (
        AuthorizationPolicy()
        .add(
            PolicyRule.allow(
                "broad",
                callers=[f"spiffe://{TRUST_DOMAIN}/agents/*"],
                targets=[f"spiffe://{TRUST_DOMAIN}/mcp"],
            )
        )
        .add(
            PolicyRule.deny(
                "narrow",
                callers=[f"spiffe://{TRUST_DOMAIN}/agents/critic"],
                targets=[f"spiffe://{TRUST_DOMAIN}/mcp"],
            )
        )
    )
    assert pol.is_allowed(
        _req(f"spiffe://{TRUST_DOMAIN}/agents/generator", f"spiffe://{TRUST_DOMAIN}/mcp")
    )
    d = pol.evaluate(
        _req(f"spiffe://{TRUST_DOMAIN}/agents/critic", f"spiffe://{TRUST_DOMAIN}/mcp")
    )
    assert not d.allowed
    assert d.matched_rule == "narrow"


def test_action_scoping() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r",
            callers=[f"spiffe://{TRUST_DOMAIN}/a"],
            targets=[f"spiffe://{TRUST_DOMAIN}/b"],
            actions=["read"],
        )
    )
    assert pol.is_allowed(_req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b", "read"))
    assert not pol.is_allowed(
        _req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b", "write")
    )


def test_condition_predicate() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r",
            callers=["*"],
            targets=[f"spiffe://{TRUST_DOMAIN}/b"],
            condition=lambda req: req.attributes.get("env") == "prod",
        )
    )
    assert not pol.is_allowed(_req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b"))
    ok = CallRequest(
        SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/a"),
        SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/b"),
        attributes={"env": "prod"},
    )
    assert pol.is_allowed(ok)


def test_authorize_raises() -> None:
    pol = AuthorizationPolicy()
    with pytest.raises(AuthorizationError):
        pol.authorize(_req(f"spiffe://{TRUST_DOMAIN}/a", f"spiffe://{TRUST_DOMAIN}/b"))


def test_matcher_for_parsing() -> None:
    assert isinstance(matcher_for("*"), AnyWorkload)
    assert isinstance(matcher_for(f"spiffe://{TRUST_DOMAIN}/agents/*"), PathPrefix)
    assert isinstance(matcher_for(f"spiffe://{TRUST_DOMAIN}/**"), DomainMember)
    assert isinstance(matcher_for(f"spiffe://{TRUST_DOMAIN}"), DomainMember)
    assert isinstance(matcher_for(f"spiffe://{TRUST_DOMAIN}/a/b"), ExactId)


def test_domain_member_matches_any_in_domain() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r", callers=[f"spiffe://{TRUST_DOMAIN}/**"], targets=[f"spiffe://{TRUST_DOMAIN}/b"]
        )
    )
    assert pol.is_allowed(_req(f"spiffe://{TRUST_DOMAIN}/anything", f"spiffe://{TRUST_DOMAIN}/b"))
    assert not pol.is_allowed(_req("spiffe://other.internal/x", f"spiffe://{TRUST_DOMAIN}/b"))


def test_authorizer_for_predicate() -> None:
    pol = AuthorizationPolicy().add(
        PolicyRule.allow(
            "r", callers=[f"spiffe://{TRUST_DOMAIN}/a"], targets=[f"spiffe://{TRUST_DOMAIN}/b"]
        )
    )
    pred = pol.authorizer_for(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/b"))
    assert pred(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/a"))
    assert not pred(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/c"))
