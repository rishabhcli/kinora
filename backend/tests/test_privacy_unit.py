"""Deterministic unit tests for the ``app.privacy`` subsystem (no infra/network).

Covers the full data-subject-rights surface with in-memory fakes:

* data-map coverage + invariant validation (append-only never destructive,
  credentials never exportable);
* DSAR export completeness (every exportable field contributes; credentials and
  the untouched subject excluded; deterministic digest);
* right-to-erasure across all four stores (hard-delete / anonymize / crypto-erase
  / redact), with the *other* subject left intact;
* crypto-erasure preserves the audit-chain integrity;
* legal hold blocks deletion (subject-wide raises; class-scoped skips);
* idempotent + resumable erasure (re-run is a no-op; a failed step resumes);
* the verifiable completion certificate + residual scan (clean vs. residual);
* the retention engine TTL / consent-withdrawal / hold matrix;
* consent tracking fold.

All of this runs in the default suite (needs no ``KINORA_TEST_*`` infra).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.privacy.certificate import issue_certificate
from app.privacy.clock import FixedClock
from app.privacy.consent import ConsentRecord, ConsentTracker
from app.privacy.datamap import (
    DEFAULT_DATA_MAP,
    DataMap,
    PIIField,
    default_data_map,
    merge_maps,
    subject_locators,
)
from app.privacy.enums import (
    ConsentStatus,
    ErasureState,
    ErasureStrategy,
    PIICategory,
    RetentionAction,
    StepStatus,
    StoreKind,
)
from app.privacy.erasure import plan_run
from app.privacy.errors import DataMapError, LegalHoldError
from app.privacy.retention import (
    LegalHold,
    RetentionPolicy,
    RetentionRule,
    default_retention_policy,
)
from app.privacy.service import PrivacyService
from tests.privacy_fakes import (
    FakeAuditLog,
    FakeBlobStore,
    FakeEventStore,
    FakeSubjectStore,
    make_populated_stores,
)

# ``asyncio_mode = "auto"`` (pyproject) auto-detects the ``async def`` tests, so no
# module-level marker is needed (a blanket one would wrongly tag the sync tests).

CONSENT_PURPOSES = ("adaptation", "analytics", "personalization")


def _service(
    stores: dict, *, clock: FixedClock, consent: ConsentTracker | None = None
) -> PrivacyService:
    return PrivacyService(
        subject_store=stores["subject_store"],
        blob_store=stores["blob_store"],
        event_store=stores["event_store"],
        audit_log=stores["audit_log"],
        consent=consent,
        consent_purposes=CONSENT_PURPOSES,
        clock=clock,
    )


async def _count(ss: FakeSubjectStore, resource: str, locator: str, sid: str) -> int:
    """Shorthand for the verbose ``count_subject_rows`` keyword call in assertions."""
    return await ss.count_subject_rows(
        resource=resource, subject_locator=locator, subject_id=sid
    )


# --------------------------------------------------------------------------- #
# 1. Data-map coverage + invariants                                           #
# --------------------------------------------------------------------------- #


def test_datamap_covers_every_store_and_retention_class() -> None:
    m = default_data_map()
    # All four primary stores are represented.
    assert m.stores() == {
        StoreKind.RELATIONAL,
        StoreKind.OBJECT_STORE,
        StoreKind.EVENT_STORE,
        StoreKind.AUDIT_LOG,
    }
    # Every field has a non-empty retention class + subject locator.
    for f in m:
        assert f.retention_class
        assert f.subject_locator
    # The default retention policy names a rule for every mapped class.
    policy = default_retention_policy()
    for rc in m.retention_classes():
        assert policy.rule(rc) is not None, f"no retention rule for {rc!r}"


def test_datamap_article30_record_is_serialisable_and_keyed() -> None:
    rows = default_data_map().article30_record()
    assert len(rows) == len(DEFAULT_DATA_MAP)
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys))  # unique
    # Credentials are flagged non-exportable in the record.
    creds = [r for r in rows if r["category"] == PIICategory.CREDENTIAL.value]
    assert creds and all(not r["exportable"] for r in creds)


def test_datamap_rejects_destructive_strategy_on_append_only_store() -> None:
    with pytest.raises(DataMapError):
        PIIField(
            store=StoreKind.AUDIT_LOG,
            resource="auth",
            field="email",
            category=PIICategory.DIRECT_IDENTIFIER,
            retention_class="audit_log",
            erasure=ErasureStrategy.HARD_DELETE,  # illegal on append-only
            subject_locator="subject_id",
        )


def test_datamap_rejects_exportable_credential() -> None:
    with pytest.raises(DataMapError):
        PIIField(
            store=StoreKind.RELATIONAL,
            resource="users",
            field="password_hash",
            category=PIICategory.CREDENTIAL,
            retention_class="account",
            erasure=ErasureStrategy.ANONYMIZE,
            subject_locator="id",
            exportable=True,  # illegal: would leak a secret
        )


def test_datamap_rejects_duplicate_field_key() -> None:
    f = PIIField(
        store=StoreKind.RELATIONAL,
        resource="users",
        field="email",
        category=PIICategory.DIRECT_IDENTIFIER,
        retention_class="account",
        erasure=ErasureStrategy.ANONYMIZE,
        subject_locator="id",
    )
    with pytest.raises(DataMapError):
        DataMap(fields=(f, f))


def test_subject_locators_detects_conflict() -> None:
    fields = [
        PIIField(
            store=StoreKind.RELATIONAL, resource="t", field="a",
            category=PIICategory.BEHAVIOURAL, retention_class="rc",
            erasure=ErasureStrategy.HARD_DELETE, subject_locator="user_id",
        ),
        PIIField(
            store=StoreKind.RELATIONAL, resource="t", field="b",
            category=PIICategory.BEHAVIOURAL, retention_class="rc",
            erasure=ErasureStrategy.HARD_DELETE, subject_locator="owner_id",
        ),
    ]
    with pytest.raises(DataMapError):
        subject_locators(fields)


def test_merge_maps_validates_combined() -> None:
    part = DataMap(
        fields=(
            PIIField(
                store=StoreKind.DERIVED_INDEX, resource="search_idx", field="name",
                category=PIICategory.DIRECT_IDENTIFIER, retention_class="account",
                erasure=ErasureStrategy.HARD_DELETE, subject_locator="user_id",
            ),
        )
    )
    merged = merge_maps(DEFAULT_DATA_MAP, part)
    assert len(merged) == len(DEFAULT_DATA_MAP) + 1
    assert StoreKind.DERIVED_INDEX in merged.stores()


# --------------------------------------------------------------------------- #
# 2. DSAR export completeness                                                 #
# --------------------------------------------------------------------------- #


async def test_dsar_export_is_complete_and_excludes_credentials() -> None:
    stores = make_populated_stores()
    clock = FixedClock()
    consent = ConsentTracker(clock=clock)
    consent.grant(subject_id=stores["subject_id"], purpose="adaptation")
    svc = _service(stores, clock=clock, consent=consent)

    bundle = await svc.export_subject(subject_id=stores["subject_id"])

    # Coverage: every *exportable* data-map field key is accounted for.
    exportable_keys = {f.key for f in DEFAULT_DATA_MAP.exportable()}
    assert set(bundle.coverage) == exportable_keys
    # The credential field is NOT in the coverage (non-exportable).
    assert "relational:users:password_hash" not in bundle.coverage

    # No exported relational record leaks the password hash.
    rel = [s for s in bundle.sections if s.store is StoreKind.RELATIONAL]
    for section in rel:
        for rec in section.records:
            assert "password_hash" not in rec

    # The subject's books, sessions, blobs, streams + audit summary all appear.
    by_resource = {(s.store, s.resource): s for s in bundle.sections}
    assert by_resource[(StoreKind.RELATIONAL, "books")].records  # owned book
    assert by_resource[(StoreKind.OBJECT_STORE, "clips/{book_id}")].records
    assert by_resource[(StoreKind.EVENT_STORE, "reading.session_recorded")].records
    # Consent snapshot rides along.
    statuses = {c.purpose: c.status for c in bundle.consent}
    assert statuses["adaptation"] is ConsentStatus.GRANTED
    assert statuses["analytics"] is ConsentStatus.NEVER


async def test_dsar_export_digest_is_deterministic() -> None:
    clock = FixedClock()
    a = await _service(make_populated_stores(), clock=clock).export_subject(subject_id="user-1")
    b = await _service(make_populated_stores(), clock=clock).export_subject(subject_id="user-1")
    assert a.digest == b.digest
    assert a.to_portable_dict()["format"] == "kinora.dsar.export/v1"


async def test_dsar_export_only_returns_the_requesting_subject() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    bundle = await svc.export_subject(subject_id=stores["subject_id"])
    # No record carries the other subject's id.
    other = stores["other"]
    for section in bundle.sections:
        for rec in section.records:
            assert other not in str(rec)


# --------------------------------------------------------------------------- #
# 3. Right-to-erasure across stores                                           #
# --------------------------------------------------------------------------- #


async def test_erasure_clears_subject_across_all_stores() -> None:
    stores = make_populated_stores()
    clock = FixedClock()
    consent = ConsentTracker(clock=clock)
    consent.grant(subject_id=stores["subject_id"], purpose="adaptation")
    svc = _service(stores, clock=clock, consent=consent)
    sid = stores["subject_id"]

    run = await svc.erase_subject(subject_id=sid)

    assert run.state is ErasureState.COMPLETED
    assert run.affected_total > 0
    # Relational: books/sessions/prefs hard-deleted; user row anonymised in place.
    ss: FakeSubjectStore = stores["subject_store"]
    assert await _count(ss, "books", "owner_id", sid) == 0
    assert await _count(ss, "reading_sessions", "user_id", sid) == 0
    users_for_sid = await ss.fetch_subject_rows(
        resource="users", subject_locator="id", subject_id=sid
    )
    assert users_for_sid == []  # anonymise detaches the row from the subject id
    # Object store: subject blobs gone.
    bs: FakeBlobStore = stores["blob_store"]
    assert await bs.list_prefix(f"clips/{sid}") == []
    assert await bs.list_prefix(f"books/{sid}") == []
    # Event store: crypto-erased (unrecoverable).
    es: FakeEventStore = stores["event_store"]
    assert await es.subject_recoverable(subject_id=sid) is False
    # Audit log: subject's PII redacted.
    al: FakeAuditLog = stores["audit_log"]
    assert await al.scan_subject(subject_id=sid) == 0
    # Consent records for the subject purged.
    assert consent.records_for(sid) == []


async def test_erasure_leaves_other_subjects_intact() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    other = stores["other"]
    await svc.erase_subject(subject_id=stores["subject_id"])

    ss: FakeSubjectStore = stores["subject_store"]
    assert await _count(ss, "users", "id", other) == 1
    assert await _count(ss, "books", "owner_id", other) == 1
    assert await stores["blob_store"].list_prefix(f"books/{other}")
    assert await stores["event_store"].subject_recoverable(subject_id=other) is True
    assert await stores["audit_log"].scan_subject(subject_id=other) == 1


# --------------------------------------------------------------------------- #
# 4. Crypto-erasure preserves the audit-chain integrity                       #
# --------------------------------------------------------------------------- #


async def test_audit_redaction_preserves_chain_integrity() -> None:
    stores = make_populated_stores()
    al: FakeAuditLog = stores["audit_log"]
    assert await al.verify_chain() is True
    hashes_before = [e.entry_hash for e in al.entries]

    svc = _service(stores, clock=FixedClock())
    run = await svc.erase_subject(subject_id=stores["subject_id"])

    # Chain still verifies *after* redaction (the whole point).
    assert await al.verify_chain() is True
    assert run.certificate is not None and run.certificate.chain_intact is True
    # The redacted entries' hashes changed (content changed) but the chain re-derived.
    hashes_after = [e.entry_hash for e in al.entries]
    assert hashes_after != hashes_before
    # No entry was deleted — the *count* is unchanged (append-only).
    assert len(al.entries) == 4
    # The non-subject system entry is untouched.
    assert al.entries[-1].content == {"event": "system_boot"}


async def test_orchestrator_raises_on_broken_chain() -> None:
    """If a (mis)redactor breaks the chain, the orchestrator surfaces it."""

    class BrokenAuditLog(FakeAuditLog):
        async def verify_chain(self) -> bool:
            return False

    stores = make_populated_stores()
    broken = BrokenAuditLog()
    broken.append(subject_id=stores["subject_id"], content={"email": "a@x.io", "ip": "1.2.3.4"})
    stores["audit_log"] = broken
    svc = _service(stores, clock=FixedClock())
    from app.privacy.errors import ChainIntegrityError

    with pytest.raises(ChainIntegrityError):
        await svc.erase_subject(subject_id=stores["subject_id"])


# --------------------------------------------------------------------------- #
# 5. Legal hold blocks deletion                                               #
# --------------------------------------------------------------------------- #


async def test_subject_wide_legal_hold_blocks_entire_erasure() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    sid = stores["subject_id"]
    hold = LegalHold(id="hold-1", subject_id=sid, data_class=None, reason="litigation")

    with pytest.raises(LegalHoldError) as exc:
        await svc.erase_subject(subject_id=sid, holds=[hold])
    assert exc.value.hold_id == "hold-1"

    # Nothing was deleted.
    ss: FakeSubjectStore = stores["subject_store"]
    assert await _count(ss, "books", "owner_id", sid) == 1
    assert await stores["event_store"].subject_recoverable(subject_id=sid) is True


async def test_class_scoped_legal_hold_skips_only_that_class() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    sid = stores["subject_id"]
    # Hold only the uploaded books; everything else should still erase.
    hold = LegalHold(id="hold-2", subject_id=sid, data_class="uploaded_book")

    run = await svc.erase_subject(subject_id=sid, holds=[hold], certify=False)

    # The book-related steps are skipped...
    held = ("books", "books/{book_id}/source.pdf")
    book_steps = [s for s in run.steps if s.resource in held]
    assert book_steps and all(s.status is StepStatus.SKIPPED for s in book_steps)
    # ...the books survive...
    ss: FakeSubjectStore = stores["subject_store"]
    assert await _count(ss, "books", "owner_id", sid) == 1
    # ...but the reading sessions (a different class) are gone.
    assert await _count(ss, "reading_sessions", "user_id", sid) == 0
    # A residual scan therefore is NOT clean (held data remains).
    scan = await svc.scan_subject(subject_id=sid)
    assert scan.clean is False


# --------------------------------------------------------------------------- #
# 6. Idempotent + resumable erasure                                           #
# --------------------------------------------------------------------------- #


async def test_erasure_is_idempotent() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    sid = stores["subject_id"]
    first = await svc.erase_subject(subject_id=sid)
    assert first.state is ErasureState.COMPLETED
    # A fresh run over already-erased stores still completes, affecting nothing new.
    second = await svc.erase_subject(subject_id=sid)
    assert second.state is ErasureState.COMPLETED
    assert second.affected_total == 0
    assert second.certificate is not None and second.certificate.complete


async def test_failed_step_is_resumable() -> None:
    """A store that errors once leaves its step PENDING; the run resumes cleanly."""
    stores = make_populated_stores()
    sid = stores["subject_id"]

    class FlakyBlobStore(FakeBlobStore):
        def __init__(self, keys: list[str]) -> None:
            super().__init__(keys)
            self.fail_next = True

        async def delete_prefix(self, prefix: str) -> int:
            if self.fail_next and prefix.startswith("clips/"):
                self.fail_next = False
                raise RuntimeError("transient S3 error")
            return await super().delete_prefix(prefix)

    flaky = FlakyBlobStore(list(stores["blob_store"].keys))
    stores["blob_store"] = flaky
    svc = _service(stores, clock=FixedClock())

    run = await svc.erase_subject(subject_id=sid)
    assert run.state is ErasureState.FAILED
    # The clips step failed; it is recorded so the run can resume.
    clips_step = next(s for s in run.steps if s.resource == "clips/{book_id}")
    assert clips_step.status is StepStatus.FAILED

    # Resume: pass the SAME run back; only the failed/pending steps replay.
    failed_step_count = sum(1 for s in run.steps if s.status is StepStatus.FAILED)
    # Reset the failed step to pending to model a retry of the recorded run.
    for s in run.steps:
        if s.status is StepStatus.FAILED:
            s.status = StepStatus.PENDING
            s.error = None
    resumed = await svc.erase_subject(subject_id=sid, run=run)
    assert failed_step_count == 1
    assert resumed.state is ErasureState.COMPLETED
    assert await flaky.list_prefix(f"clips/{sid}") == []


async def test_plan_run_is_deterministic_one_step_per_store_resource_strategy() -> None:
    run = plan_run("user-9")
    keys = [s.key for s in run.steps]
    assert keys == sorted(set(keys), key=keys.index)  # no duplicates, order preserved
    # Append-only stores only carry chain-preserving strategies.
    for s in run.steps:
        if s.store in (StoreKind.EVENT_STORE, StoreKind.AUDIT_LOG):
            assert s.strategy in (ErasureStrategy.CRYPTO_ERASE, ErasureStrategy.REDACT)


# --------------------------------------------------------------------------- #
# 7. Completion certificate + residual scan                                   #
# --------------------------------------------------------------------------- #


async def test_certificate_is_complete_and_verifiable_after_clean_erasure() -> None:
    stores = make_populated_stores()
    clock = FixedClock()
    svc = _service(stores, clock=clock)
    sid = stores["subject_id"]
    await svc.erase_subject(subject_id=sid, certify=False)

    cert = await svc.certificate(subject_id=sid)
    assert cert.complete is True
    assert cert.chain_intact is True
    assert all(v == 0 for v in cert.per_store_residual.values())
    assert cert.verify() is True
    assert cert.to_dict()["format"] == "kinora.privacy.erasure_certificate/v1"


async def test_certificate_tamper_is_detected() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    sid = stores["subject_id"]
    await svc.erase_subject(subject_id=sid, certify=False)
    cert = await svc.certificate(subject_id=sid)
    # Tamper with the residual counts without recomputing the hash.
    import dataclasses

    forged = dataclasses.replace(
        cert, per_store_residual={**cert.per_store_residual, "relational": 99}
    )
    assert forged.verify() is False


async def test_residual_scan_flags_remaining_data_before_erasure() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    scan = await svc.scan_subject(subject_id=stores["subject_id"])
    assert scan.total_residual > 0
    assert scan.clean is False
    # The certificate minted from a dirty scan is NOT complete.
    cert = issue_certificate(scan)
    assert cert.complete is False


async def test_erasure_with_certify_completes_only_when_clean() -> None:
    stores = make_populated_stores()
    svc = _service(stores, clock=FixedClock())
    run = await svc.erase_subject(subject_id=stores["subject_id"], certify=True)
    assert run.state is ErasureState.COMPLETED
    assert run.certificate is not None and run.certificate.complete is True


# --------------------------------------------------------------------------- #
# 8. Retention engine matrix                                                  #
# --------------------------------------------------------------------------- #


def test_retention_retains_within_ttl_and_expires_past_it() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    policy = RetentionPolicy.from_rules(
        [RetentionRule(data_class="reading_session", ttl_days=30)], clock=clock
    )
    created = datetime(2026, 1, 1, tzinfo=UTC)
    within = policy.evaluate(data_class="reading_session", subject_id="u", created_at=created)
    assert within.action is RetentionAction.RETAIN
    clock.advance(days=31)
    past = policy.evaluate(data_class="reading_session", subject_id="u", created_at=created)
    assert past.action is RetentionAction.EXPIRE


def test_retention_none_ttl_retains_for_account_life() -> None:
    policy = default_retention_policy()
    decision = policy.evaluate(
        data_class="account", subject_id="u", created_at=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert decision.action is RetentionAction.RETAIN
    assert decision.expires_at is None


def test_retention_legal_hold_blocks_expiry() -> None:
    clock = FixedClock()
    policy = RetentionPolicy.from_rules(
        [RetentionRule(data_class="reading_session", ttl_days=0)], clock=clock
    )
    hold = LegalHold(id="h", subject_id="u", data_class="reading_session")
    decision = policy.evaluate(
        data_class="reading_session", subject_id="u",
        created_at=clock(), holds=[hold],
    )
    assert decision.action is RetentionAction.BLOCKED_BY_HOLD
    assert decision.hold_id == "h"


def test_retention_consent_withdrawal_expires_consent_based_class() -> None:
    policy = default_retention_policy()
    decision = policy.evaluate(
        data_class="directing_preference", subject_id="u",
        created_at=datetime(2026, 6, 1, tzinfo=UTC), consent_withdrawn=True,
    )
    assert decision.action is RetentionAction.EXPIRE
    assert "consent withdrawn" in decision.reason


def test_retention_is_blocked_helper() -> None:
    policy = default_retention_policy()
    hold = LegalHold(id="h", subject_id="u")  # subject-wide
    assert policy.is_blocked(subject_id="u", data_class="account", holds=[hold]) is hold
    lifted = LegalHold(id="h2", subject_id="u", active=False)
    assert policy.is_blocked(subject_id="u", data_class="account", holds=[lifted]) is None


def test_default_retention_overrides_apply() -> None:
    policy = default_retention_policy(overrides={"reading_session": 7})
    rule = policy.rule("reading_session")
    assert rule is not None and rule.ttl_days == 7


# --------------------------------------------------------------------------- #
# 9. Consent tracking fold                                                     #
# --------------------------------------------------------------------------- #


def test_consent_fold_last_action_wins() -> None:
    clock = FixedClock()
    t = ConsentTracker(clock=clock)
    t.grant(subject_id="u", purpose="analytics")
    clock.advance(days=1)
    t.withdraw(subject_id="u", purpose="analytics")
    status = t.status(subject_id="u", purpose="analytics")
    assert status.status is ConsentStatus.WITHDRAWN
    assert t.has_consent(subject_id="u", purpose="analytics") is False


def test_consent_never_for_unseen_purpose() -> None:
    t = ConsentTracker()
    assert t.status(subject_id="u", purpose="marketing").status is ConsentStatus.NEVER


def test_consent_proof_trail_is_ordered_and_purgeable() -> None:
    clock = FixedClock()
    t = ConsentTracker.from_records(
        [
            ConsentRecord("u", "analytics", True, datetime(2026, 1, 2, tzinfo=UTC)),
            ConsentRecord("u", "analytics", False, datetime(2026, 1, 1, tzinfo=UTC)),
        ],
        clock=clock,
    )
    trail = t.records_for("u")
    assert [r.granted for r in trail] == [False, True]  # ordered oldest-first
    assert t.purge_subject("u") == 2
    assert t.purge_subject("u") == 0  # idempotent
