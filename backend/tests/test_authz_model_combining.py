"""Unit tests for the authz-plane core model + combining algorithms (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.platform.authz.combining import CombiningAlgorithm, combine
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Effect,
    EngineResult,
    Obligation,
    Reason,
    Resource,
    Subject,
)


def _req(action: str = "book:read") -> AuthorizationRequest:
    return AuthorizationRequest(
        subject=Subject.user("alice"),
        action=action,
        resource=Resource.of("book", "42"),
    )


def test_subject_refs_and_roles() -> None:
    s = Subject.user("alice", roles=["editor", "reader"], tenant="t1")
    assert s.ref == "user:alice"
    assert s.roles == frozenset({"editor", "reader"})
    assert s.tenant == "t1"
    # a single string role is normalised to a one-element set
    assert Subject.user("bob", roles="admin").roles == frozenset({"admin"})


def test_resource_owner_tenant_and_type_level() -> None:
    r = Resource.of("book", "42", owner="alice", tenant="t1")
    assert r.ref == "book:42"
    assert r.owner == "alice"
    assert r.tenant == "t1"
    assert Resource.type_level("book").id == "*"


def test_attributes_are_frozen_copies() -> None:
    attrs = {"roles": ["editor"]}
    s = Subject(type="user", id="x", attributes=attrs)
    attrs["roles"] = ["admin"]  # mutate the original
    assert s.roles == frozenset({"editor"})  # the subject kept its own copy


def test_cache_key_stable_and_ignores_now() -> None:
    a = AuthorizationRequest(
        subject=Subject.user("alice", roles=["editor"]),
        action="book:read",
        resource=Resource.of("book", "42", owner="alice"),
        context=Context(attributes={"ip": "1.2.3.4"}, now=datetime(2020, 1, 1, tzinfo=UTC)),
    )
    b = AuthorizationRequest(
        subject=Subject.user("alice", roles=["editor"]),
        action="book:read",
        resource=Resource.of("book", "42", owner="alice"),
        context=Context(attributes={"ip": "1.2.3.4"}, now=datetime(2099, 1, 1, tzinfo=UTC)),
    )
    assert a.cache_key == b.cache_key  # `now` excluded
    c = AuthorizationRequest(
        subject=Subject.user("bob"),
        action="book:read",
        resource=Resource.of("book", "42"),
    )
    assert a.cache_key != c.cache_key


def test_effect_is_decisive() -> None:
    assert Effect.ALLOW.is_decisive
    assert Effect.DENY.is_decisive
    assert not Effect.ABSTAIN.is_decisive


def test_engine_result_constructors() -> None:
    allow = EngineResult.allow("rbac", "ok", rule="r1")
    assert allow.effect is Effect.ALLOW and allow.reasons[0].rule == "r1"
    deny = EngineResult.deny("abac", "no")
    assert deny.effect is Effect.DENY
    abstain = EngineResult.abstain("policy")
    assert abstain.effect is Effect.ABSTAIN


# -- combining algorithms ---------------------------------------------------- #


def test_deny_overrides_deny_beats_allow() -> None:
    results = [EngineResult.allow("rbac", "ok"), EngineResult.deny("abac", "no")]
    decision = combine(_req(), results, algorithm=CombiningAlgorithm.DENY_OVERRIDES)
    assert decision.effect is Effect.DENY
    # the full trail is preserved even though deny won
    assert len(decision.reasons) == 2


def test_deny_overrides_allow_when_no_deny() -> None:
    results = [EngineResult.abstain("rbac"), EngineResult.allow("abac", "ok")]
    decision = combine(_req(), results, algorithm=CombiningAlgorithm.DENY_OVERRIDES)
    assert decision.effect is Effect.ALLOW
    assert decision.allowed


def test_deny_overrides_all_abstain_defaults_deny() -> None:
    results = [EngineResult.abstain("rbac"), EngineResult.abstain("abac")]
    decision = combine(_req(), results, algorithm=CombiningAlgorithm.DENY_OVERRIDES)
    assert decision.effect is Effect.DENY
    assert not decision.allowed


def test_permit_overrides_allow_beats_deny() -> None:
    results = [EngineResult.deny("abac", "no"), EngineResult.allow("rbac", "ok")]
    decision = combine(_req(), results, algorithm=CombiningAlgorithm.PERMIT_OVERRIDES)
    assert decision.effect is Effect.ALLOW


def test_first_applicable_takes_first_decisive() -> None:
    results = [
        EngineResult.abstain("rbac"),
        EngineResult.deny("abac", "no"),
        EngineResult.allow("policy", "ok"),
    ]
    decision = combine(_req(), results, algorithm=CombiningAlgorithm.FIRST_APPLICABLE)
    assert decision.effect is Effect.DENY  # the deny is first decisive


def test_deny_unless_permit_closed_world() -> None:
    only_abstain = [EngineResult.abstain("rbac")]
    assert combine(
        _req(), only_abstain, algorithm=CombiningAlgorithm.DENY_UNLESS_PERMIT
    ).effect is Effect.DENY
    with_allow = [EngineResult.allow("rbac", "ok")]
    assert combine(
        _req(), with_allow, algorithm=CombiningAlgorithm.DENY_UNLESS_PERMIT
    ).effect is Effect.ALLOW


def test_obligations_carried_only_on_allow() -> None:
    ob = Obligation(name="redact", parameters={"field": "ssn"})
    allow = EngineResult.allow("policy", "ok", obligations=[ob])
    decision = combine(_req(), [allow])
    assert decision.obligations == (ob,)
    # but a deny drops obligations
    decision2 = combine(_req(), [allow, EngineResult.deny("abac", "no")])
    assert decision2.effect is Effect.DENY
    assert decision2.obligations == ()


def test_empty_results_annotated() -> None:
    decision = combine(_req(), [])
    assert decision.effect is Effect.DENY
    assert decision.reasons[0].source == "combiner"


def test_decision_explanation_renders_all_reasons() -> None:
    results = [
        EngineResult(effect=Effect.ALLOW, reasons=(Reason("rbac", Effect.ALLOW, "role"),)),
        EngineResult(effect=Effect.DENY, reasons=(Reason("abac", Effect.DENY, "tenant"),)),
    ]
    decision = combine(_req(), results)
    text = decision.explanation
    assert "rbac" in text and "abac" in text
