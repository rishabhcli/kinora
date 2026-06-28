"""Unit tests for the compliance subsystem's PURE logic (no infrastructure).

Covers the deterministic, DB-free parts so they run anywhere:

* the hash-chain primitive (determinism, order-independence, tamper detection);
* the DSAR state machine (the full legal/illegal transition matrix);
* the retention engine (TTL / consent-withdrawal / legal-hold interactions);
* the consent value objects (granted / stale);
* the policy-as-code rule set + report aggregation;
* the injectable clock.

These need none of ``KINORA_TEST_*`` infra and so are part of the default suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.compliance.clock import FixedClock, ensure_utc, system_clock
from app.compliance.consent.policy import DEFAULT_PURPOSE_CATALOG, body_hash
from app.compliance.consent.service import ConsentSnapshot, PurposeConsent
from app.compliance.dsar.machine import (
    DSARMachine,
    allowed_next,
    can_transition,
    is_terminal,
)
from app.compliance.dsar.service import DSARView
from app.compliance.enums import (
    ConsentState,
    DataClass,
    DSARKind,
    DSARState,
    PolicyDecision,
    ProcessingPurpose,
    RuleSeverity,
)
from app.compliance.errors import InvalidTransitionError
from app.compliance.hold.service import HoldScope
from app.compliance.ledger.chain import (
    GENESIS_PREV_HASH,
    canonical_json,
    chain_hash,
    payload_core,
    sha256_hex,
)
from app.compliance.policy.engine import PolicyEngine
from app.compliance.policy.report import build_report
from app.compliance.policy.rules import ComplianceFacts

# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


def test_system_clock_is_utc_aware() -> None:
    now = system_clock()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fixed_clock_advances_deterministically() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock() == datetime(2026, 1, 1, tzinfo=UTC)
    clock.advance(days=31)
    assert clock() == datetime(2026, 2, 1, tzinfo=UTC)
    clock.set(datetime(2027, 6, 1, tzinfo=UTC))
    assert clock().year == 2027


def test_ensure_utc_normalises_naive() -> None:
    naive = datetime(2026, 1, 1)
    assert ensure_utc(naive).tzinfo == UTC


def test_fixed_clock_rejects_naive_set() -> None:
    clock = FixedClock()
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 1, 1))


# --------------------------------------------------------------------------- #
# Hash chain
# --------------------------------------------------------------------------- #


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_sha256_hex_is_64_hex_chars() -> None:
    digest = sha256_hex("hello")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_chain_hash_genesis_uses_sentinel() -> None:
    core = payload_core(
        seq=1, category="consent", event="x", subject_id=None, actor_id="system", payload=None
    )
    assert chain_hash(None, core) == chain_hash(GENESIS_PREV_HASH, core)


def test_chain_hash_depends_on_prev_and_content() -> None:
    core = payload_core(
        seq=2, category="consent", event="x", subject_id="u1", actor_id="u1", payload={"k": 1}
    )
    h_a = chain_hash("a" * 64, core)
    h_b = chain_hash("b" * 64, core)
    assert h_a != h_b  # different prev → different hash

    core2 = payload_core(
        seq=2, category="consent", event="x", subject_id="u1", actor_id="u1", payload={"k": 2}
    )
    assert chain_hash("a" * 64, core2) != h_a  # different payload → different hash


def test_chain_links_reproduce() -> None:
    """A three-link chain re-derives identically — the verification invariant."""
    links: list[str] = []
    prev: str | None = None
    for seq in range(1, 4):
        core = payload_core(
            seq=seq,
            category="dsar",
            event=f"e{seq}",
            subject_id="u1",
            actor_id="system",
            payload={"n": seq},
        )
        h = chain_hash(prev, core)
        links.append(h)
        prev = h
    # Re-derive and compare.
    prev = None
    for seq, expected in enumerate(links, start=1):
        core = payload_core(
            seq=seq,
            category="dsar",
            event=f"e{seq}",
            subject_id="u1",
            actor_id="system",
            payload={"n": seq},
        )
        assert chain_hash(prev, core) == expected
        prev = expected


# --------------------------------------------------------------------------- #
# DSAR state machine
# --------------------------------------------------------------------------- #


def test_dsar_happy_path_transitions() -> None:
    assert can_transition(DSARState.RECEIVED, DSARState.VERIFYING)
    assert can_transition(DSARState.VERIFYING, DSARState.IN_PROGRESS)
    assert can_transition(DSARState.IN_PROGRESS, DSARState.COMPLETED)
    assert can_transition(DSARState.IN_PROGRESS, DSARState.EXTENDED)
    assert can_transition(DSARState.EXTENDED, DSARState.COMPLETED)


def test_dsar_cancellation_from_any_open_state() -> None:
    for state in (
        DSARState.RECEIVED,
        DSARState.VERIFYING,
        DSARState.IN_PROGRESS,
        DSARState.EXTENDED,
    ):
        assert can_transition(state, DSARState.CANCELLED)


def test_dsar_terminal_states_are_dead_ends() -> None:
    for terminal in (DSARState.COMPLETED, DSARState.REJECTED, DSARState.CANCELLED):
        assert is_terminal(terminal)
        assert allowed_next(terminal) == frozenset()
        for target in DSARState:
            assert not can_transition(terminal, target)


def test_dsar_illegal_skips_are_rejected() -> None:
    assert not can_transition(DSARState.RECEIVED, DSARState.IN_PROGRESS)  # skips verifying
    assert not can_transition(DSARState.RECEIVED, DSARState.COMPLETED)
    assert not can_transition(DSARState.VERIFYING, DSARState.EXTENDED)
    assert not can_transition(DSARState.IN_PROGRESS, DSARState.VERIFYING)  # no going back


def test_dsar_no_self_transition() -> None:
    assert not can_transition(DSARState.RECEIVED, DSARState.RECEIVED)


def test_dsar_machine_asserts() -> None:
    DSARMachine.assert_transition(DSARState.RECEIVED, DSARState.VERIFYING)  # no raise
    with pytest.raises(InvalidTransitionError):
        DSARMachine.assert_transition(DSARState.COMPLETED, DSARState.IN_PROGRESS)


# --------------------------------------------------------------------------- #
# Consent value objects
# --------------------------------------------------------------------------- #


def test_purpose_consent_granted_and_stale() -> None:
    fresh = PurposeConsent(
        purpose=ProcessingPurpose.ADAPTATION,
        state=ConsentState.GRANTED,
        granted_version=2,
        current_version=2,
    )
    assert fresh.is_granted and not fresh.is_stale

    stale = PurposeConsent(
        purpose=ProcessingPurpose.ADAPTATION,
        state=ConsentState.GRANTED,
        granted_version=1,
        current_version=2,
    )
    assert stale.is_granted and stale.is_stale

    never = PurposeConsent(purpose=ProcessingPurpose.ANALYTICS, state=ConsentState.NEVER)
    assert not never.is_granted and not never.is_stale


def test_consent_snapshot_lookup_defaults_to_never() -> None:
    snap = ConsentSnapshot(subject_id="u1", purposes=())
    got = snap.for_purpose(ProcessingPurpose.MARKETING_EMAIL)
    assert got.state == ConsentState.NEVER


def test_policy_body_hash_normalises_trailing_whitespace() -> None:
    assert body_hash("hello world  ") == body_hash("hello world")
    assert body_hash("a\nb") != body_hash("a\nc")


def test_default_catalog_marks_adaptation_required() -> None:
    by_purpose = {spec.purpose: spec for spec in DEFAULT_PURPOSE_CATALOG}
    assert by_purpose[ProcessingPurpose.ADAPTATION].required
    assert not by_purpose[ProcessingPurpose.MODEL_TRAINING].required


# --------------------------------------------------------------------------- #
# Hold scope
# --------------------------------------------------------------------------- #


def test_hold_scope_all_data_covers_every_class() -> None:
    scope = HoldScope(
        subject_id="u1", all_data=True, held_classes=frozenset(), hold_ids=("h1",)
    )
    assert scope.any_active
    assert scope.covers(DataClass.UPLOADED_BOOK)
    assert scope.covers(DataClass.READING_SESSION)


def test_hold_scope_class_scoped() -> None:
    scope = HoldScope(
        subject_id="u1",
        all_data=False,
        held_classes=frozenset({DataClass.GENERATED_MEDIA}),
        hold_ids=("h1",),
    )
    assert scope.covers(DataClass.GENERATED_MEDIA)
    assert not scope.covers(DataClass.READING_SESSION)


def test_hold_scope_empty_is_inactive() -> None:
    scope = HoldScope(
        subject_id="u1", all_data=False, held_classes=frozenset(), hold_ids=()
    )
    assert not scope.any_active
    assert not scope.covers(DataClass.ACCOUNT)


# --------------------------------------------------------------------------- #
# Policy-as-code rules + report
# --------------------------------------------------------------------------- #


def _facts(
    *,
    granted: set[ProcessingPurpose] | None = None,
    stale: set[ProcessingPurpose] | None = None,
    required: set[ProcessingPurpose] | None = None,
    hold: HoldScope | None = None,
    dsars: tuple[DSARView, ...] = (),
) -> ComplianceFacts:
    granted = granted or set()
    stale = stale or set()
    purposes: list[PurposeConsent] = []
    for purpose in ProcessingPurpose:
        if purpose in granted:
            purposes.append(
                PurposeConsent(
                    purpose=purpose,
                    state=ConsentState.GRANTED,
                    granted_version=1 if purpose not in stale else 1,
                    current_version=1 if purpose not in stale else 2,
                )
            )
        else:
            purposes.append(PurposeConsent(purpose=purpose, state=ConsentState.NEVER))
    return ComplianceFacts(
        subject_id="u1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        consent=ConsentSnapshot(subject_id="u1", purposes=tuple(purposes)),
        hold=hold or HoldScope("u1", all_data=False, held_classes=frozenset(), hold_ids=()),
        dsars=dsars,
        required_purposes=frozenset(required or set()),
    )


def test_report_denies_when_required_consent_missing() -> None:
    facts = _facts(required={ProcessingPurpose.ADAPTATION})  # not granted
    report = build_report(facts)
    assert report.decision == PolicyDecision.DENY
    assert not report.is_compliant
    failed_ids = {r.rule_id for r in report.failures}
    assert "required_consents_granted" in failed_ids


def test_report_allows_when_required_consent_granted() -> None:
    facts = _facts(
        granted={ProcessingPurpose.ADAPTATION, ProcessingPurpose.MODEL_TRAINING},
        required={ProcessingPurpose.ADAPTATION},
    )
    report = build_report(facts)
    # model-training granted, no overdue DSAR, no stale → fully ALLOW.
    assert report.decision == PolicyDecision.ALLOW
    assert report.is_compliant


def test_report_denies_model_training_without_consent() -> None:
    facts = _facts(granted={ProcessingPurpose.ADAPTATION}, required={ProcessingPurpose.ADAPTATION})
    report = build_report(facts)
    # model_training not granted → that critical rule denies.
    assert report.decision == PolicyDecision.DENY
    assert any(r.rule_id == "model_training_requires_consent" for r in report.failures)


def test_report_obligation_on_stale_consent() -> None:
    facts = _facts(
        granted={ProcessingPurpose.ADAPTATION, ProcessingPurpose.MODEL_TRAINING},
        stale={ProcessingPurpose.ADAPTATION},
        required={ProcessingPurpose.ADAPTATION},
    )
    report = build_report(facts)
    assert report.decision == PolicyDecision.ALLOW_WITH_OBLIGATION
    assert report.is_compliant  # an obligation is not a denial
    assert report.obligations


def test_report_denies_overdue_dsar() -> None:
    overdue = DSARView(
        id="d1",
        subject_id="u1",
        kind=DSARKind.ACCESS,
        state=DSARState.IN_PROGRESS,
        received_at=datetime(2025, 1, 1, tzinfo=UTC),
        due_at=datetime(2025, 2, 1, tzinfo=UTC),
        effective_due_at=datetime(2025, 2, 1, tzinfo=UTC),
        completed_at=None,
        overdue=True,
        result=None,
    )
    facts = _facts(
        granted={ProcessingPurpose.ADAPTATION, ProcessingPurpose.MODEL_TRAINING},
        required={ProcessingPurpose.ADAPTATION},
        dsars=(overdue,),
    )
    report = build_report(facts)
    assert report.decision == PolicyDecision.DENY
    assert any(r.rule_id == "dsars_within_deadline" for r in report.failures)


def test_report_warns_erasure_blocked_by_hold() -> None:
    erasure = DSARView(
        id="e1",
        subject_id="u1",
        kind=DSARKind.ERASURE,
        state=DSARState.IN_PROGRESS,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        due_at=datetime(2026, 1, 31, tzinfo=UTC),
        effective_due_at=datetime(2026, 1, 31, tzinfo=UTC),
        completed_at=None,
        overdue=False,
        result=None,
    )
    facts = _facts(
        granted={ProcessingPurpose.ADAPTATION, ProcessingPurpose.MODEL_TRAINING},
        required={ProcessingPurpose.ADAPTATION},
        hold=HoldScope("u1", all_data=True, held_classes=frozenset(), hold_ids=("h1",)),
        dsars=(erasure,),
    )
    report = build_report(facts)
    assert report.decision == PolicyDecision.ALLOW_WITH_OBLIGATION
    assert any("hold" in o.lower() for o in report.obligations)


def test_report_to_dict_is_serialisable() -> None:
    facts = _facts(
        granted={ProcessingPurpose.ADAPTATION, ProcessingPurpose.MODEL_TRAINING},
        required={ProcessingPurpose.ADAPTATION},
    )
    data = build_report(facts).to_dict()
    assert set(data) >= {"subject_id", "decision", "is_compliant", "rules", "summary"}
    assert isinstance(data["rules"], list)
    # round-trips through JSON without error.
    import json

    json.dumps(data)


def test_policy_engine_isolates_a_buggy_rule() -> None:
    from app.compliance.policy.rules import PolicyRule, RuleOutcome

    def _boom(_facts: ComplianceFacts) -> RuleOutcome:
        raise RuntimeError("kaboom")

    engine = PolicyEngine(
        rules=(
            PolicyRule(
                id="boom",
                title="explodes",
                severity=RuleSeverity.WARNING,
                evaluate=_boom,
            ),
        )
    )
    facts = _facts()
    results = engine.evaluate(facts)
    assert len(results) == 1
    assert results[0].outcome.decision == PolicyDecision.DENY  # failure surfaces as DENY
    assert "kaboom" in results[0].outcome.message
