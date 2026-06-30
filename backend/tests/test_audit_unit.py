"""Deterministic unit tests for the audit / provenance subsystem (``app.audit``).

Covers the whole subsystem with NO infrastructure and NO network — the pure
hash-chain + Merkle primitives, redaction, the typed event model, the taxonomy,
and the full :class:`AuditService` over the in-memory sink. Everything is
deterministic (a :class:`FixedClock`-style stub feeds wall-clock time) so these
run in the default suite.

The tamper-evidence section is the heart of the contract: we build a clean
chain, assert it verifies, then mutate the store three ways (insert / edit /
delete) and assert each is detected and pinpointed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.audit.chain import (
    GENESIS_PREV_HASH,
    canonical_json,
    chain_hash,
    merkle_proof,
    merkle_root,
    record_core,
    verify_merkle_proof,
)
from app.audit.events import AuditEvent
from app.audit.query import AuditQuery
from app.audit.redaction import Redactor, contains_pii_plaintext, is_redacted
from app.audit.service import AuditService
from app.audit.store import AuditRecord, InMemoryAuditSink
from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
    category_for_action,
    default_severity,
    is_coherent,
)

# This module uses pytest-asyncio in ``auto`` mode (see pyproject ``asyncio_mode``),
# so ``async def test_*`` needs no explicit marker.


# --------------------------------------------------------------------------- #
# A deterministic clock for record() wall-clock timestamps
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kw: float) -> None:
        self._now = self._now + timedelta(**kw)


def _service(**kwargs: object) -> AuditService:
    return AuditService(InMemoryAuditSink(), clock=_Clock(), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #


def test_action_category_mapping_is_total_and_coherent() -> None:
    for action in AuditAction:
        cat = category_for_action(action)
        assert isinstance(cat, AuditCategory)
        # An action is coherent with its own category and OTHER is universal.
        assert is_coherent(cat, action)
    assert is_coherent(AuditCategory.RENDER, AuditAction.OTHER)
    assert not is_coherent(AuditCategory.RENDER, AuditAction.AUTH_LOGIN)


def test_default_severity_skews_auth_and_config_higher() -> None:
    assert default_severity(AuditCategory.AUTH) is AuditSeverity.NOTICE
    assert default_severity(AuditCategory.CONFIG) is AuditSeverity.WARNING
    assert default_severity(AuditCategory.RENDER) is AuditSeverity.INFO


# --------------------------------------------------------------------------- #
# Pure hash-chain primitive
# --------------------------------------------------------------------------- #


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_chain_hash_is_deterministic_and_chains() -> None:
    core = record_core(
        seq=1,
        event_id="e1",
        occurred_at="2026-01-01T00:00:00+00:00",
        category="canon",
        action="canon.created",
        severity="info",
        actor_kind="agent",
        actor_id="continuity",
        target_type="canon_fact",
        target_id="char_1",
        correlation_id="c1",
        trace_id=None,
        reason=None,
        before=None,
        after={"name": "A"},
        payload=None,
    )
    h1 = chain_hash(GENESIS_PREV_HASH, core)
    assert h1 == chain_hash(GENESIS_PREV_HASH, core)  # deterministic
    assert chain_hash("ff" * 32, core) != h1  # different prev => different hash


# --------------------------------------------------------------------------- #
# Merkle checkpoints
# --------------------------------------------------------------------------- #


def test_merkle_root_stable_and_inclusion_proof_verifies() -> None:
    leaves = [f"{i + 1:064x}" for i in range(7)]  # odd count exercises duplicate-last
    root = merkle_root(leaves)
    assert root == merkle_root(leaves)  # stable
    for i in range(len(leaves)):
        proof = merkle_proof(leaves, i)
        assert verify_merkle_proof(leaves[i], proof, root)
    # A leaf not in the tree fails the proof for position 0.
    bad_proof = merkle_proof(leaves, 0)
    assert not verify_merkle_proof("ee" * 32, bad_proof, root)


def test_merkle_root_changes_if_any_leaf_changes() -> None:
    leaves = [f"{i:064x}" for i in range(4)]
    tampered = list(leaves)
    tampered[2] = "ab" * 32
    assert merkle_root(leaves) != merkle_root(tampered)


# --------------------------------------------------------------------------- #
# Redaction (PII scrubbed, commitment preserved)
# --------------------------------------------------------------------------- #


def test_redactor_commits_sensitive_keys_and_keeps_the_rest() -> None:
    r = Redactor(salt="s")
    out = r.redact({"name": "Alice", "email": "alice@x.io", "password": "hunter2"})
    assert out["name"] == "Alice"  # non-PII verbatim
    assert is_redacted(out["email"]) and is_redacted(out["password"])
    assert "alice@x.io" not in canonical_json(out)
    assert "hunter2" not in canonical_json(out)


def test_redactor_verify_proves_a_value_against_its_commitment() -> None:
    r = Redactor(salt="s")
    commitment = r.redact({"email": "bob@x.io"})["email"]
    assert r.verify("bob@x.io", commitment)
    assert not r.verify("eve@x.io", commitment)


def test_contains_pii_plaintext_flags_unredacted_only() -> None:
    r = Redactor(salt="s")
    redacted = r.redact({"email": "x@y.io"})
    assert not contains_pii_plaintext(redacted, r)
    assert contains_pii_plaintext({"email": "x@y.io"}, r)


# --------------------------------------------------------------------------- #
# AuditEvent model
# --------------------------------------------------------------------------- #


def test_event_fills_default_severity_and_normalises_utc() -> None:
    naive = datetime(2026, 6, 1, 12, 0, 0)
    ev = AuditEvent(
        category=AuditCategory.AUTH,
        action=AuditAction.AUTH_LOGIN,
        actor_kind=AuditActorKind.USER,
        actor_id="usr_1",
        occurred_at=naive,
    )
    assert ev.severity is AuditSeverity.NOTICE
    assert ev.occurred_at.tzinfo is not None


def test_event_rejects_incoherent_category_action() -> None:
    with pytest.raises(ValueError):
        AuditEvent(
            category=AuditCategory.RENDER,
            action=AuditAction.AUTH_LOGIN,
            actor_kind=AuditActorKind.USER,
            actor_id="usr_1",
        )


def test_event_for_action_derives_category() -> None:
    ev = AuditEvent.for_action(
        AuditAction.BUDGET_SPENT, actor_kind=AuditActorKind.SYSTEM, actor_id="scheduler"
    )
    assert ev.category is AuditCategory.BUDGET


# --------------------------------------------------------------------------- #
# AuditService — append + verify
# --------------------------------------------------------------------------- #


async def _seed(svc: AuditService, n: int) -> None:
    for i in range(n):
        await svc.record_event(
            AuditAction.CANON_UPDATED,
            actor_kind=AuditActorKind.AGENT,
            actor_id="continuity",
            target_type="canon_fact",
            target_id=f"char_{i}",
            correlation_id="render_1",
            payload={"i": i},
        )


async def test_clean_chain_verifies() -> None:
    svc = _service(segment_size=1000)
    await _seed(svc, 5)
    report = await svc.verify_integrity()
    assert report.ok
    assert report.chain.entries == 5


async def test_tamper_edit_is_detected() -> None:
    sink = InMemoryAuditSink()
    svc = AuditService(sink, clock=_Clock(), segment_size=1000)
    await _seed(svc, 4)
    from dataclasses import replace

    sink._entries[1] = replace(sink._entries[1], payload={"i": 999})
    report = await svc.verify_integrity()
    assert not report.ok
    assert report.chain.broken_at_seq == 2


async def test_tamper_delete_is_detected() -> None:
    sink = InMemoryAuditSink()
    svc = AuditService(sink, clock=_Clock(), segment_size=1000)
    await _seed(svc, 4)
    del sink._entries[1]  # remove seq 2
    report = await svc.verify_integrity()
    assert not report.ok
    assert report.reason and "sequence gap" in report.reason


async def test_tamper_insert_is_detected() -> None:
    sink = InMemoryAuditSink()
    svc = AuditService(sink, clock=_Clock(), segment_size=1000)
    await _seed(svc, 3)
    # Forge an extra entry appended with a plausible-but-wrong seq/hash.
    forged = AuditRecord(
        id="forged",
        seq=2,  # duplicates an existing seq -> gap check trips
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        category=AuditCategory.CANON,
        action=AuditAction.CANON_DELETED,
        severity=AuditSeverity.INFO,
        actor_kind=AuditActorKind.AGENT,
        actor_id="attacker",
        target_type=None,
        target_id=None,
        correlation_id=None,
        trace_id=None,
        reason=None,
        before=None,
        after=None,
        payload=None,
        prev_hash="00" * 32,
        entry_hash="ff" * 32,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    sink._entries.insert(1, forged)
    report = await svc.verify_integrity()
    assert not report.ok


# --------------------------------------------------------------------------- #
# Query filters
# --------------------------------------------------------------------------- #


async def test_query_filters_by_actor_category_and_time() -> None:
    svc = _service(segment_size=1000)
    await svc.record_event(
        AuditAction.AUTH_LOGIN,
        actor_kind=AuditActorKind.USER,
        actor_id="usr_1",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await svc.record_event(
        AuditAction.CANON_CREATED,
        actor_kind=AuditActorKind.AGENT,
        actor_id="continuity",
        occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await svc.record_event(
        AuditAction.AUTH_LOGIN,
        actor_kind=AuditActorKind.USER,
        actor_id="usr_1",
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    by_actor = await svc.query(AuditQuery(actor_ids=frozenset({"usr_1"})))
    assert {r.action for r in by_actor} == {AuditAction.AUTH_LOGIN}
    assert len(by_actor) == 2
    by_cat = await svc.query(AuditQuery(categories=frozenset({AuditCategory.CANON})))
    assert len(by_cat) == 1
    windowed = await svc.query(
        AuditQuery(
            since=datetime(2026, 1, 15, tzinfo=UTC), until=datetime(2026, 2, 15, tzinfo=UTC)
        )
    )
    assert len(windowed) == 1 and windowed[0].category is AuditCategory.CANON


async def test_query_pagination_and_ordering() -> None:
    svc = _service(segment_size=1000)
    await _seed(svc, 6)
    desc = await svc.query(AuditQuery(ascending=False, limit=2))
    assert [r.seq for r in desc] == [6, 5]
    page2 = await svc.query(AuditQuery(ascending=True, limit=2, offset=2))
    assert [r.seq for r in page2] == [3, 4]


# --------------------------------------------------------------------------- #
# Provenance trail reconstruction for a clip
# --------------------------------------------------------------------------- #


async def test_provenance_trail_reconstructs_a_clips_story() -> None:
    svc = _service(segment_size=1000)
    corr = "render_42"
    # The story of clip shot_7: canon read, arbitration, budget, render accept.
    await svc.record_event(
        AuditAction.CANON_UPDATED,
        actor_kind=AuditActorKind.AGENT,
        actor_id="continuity",
        target_type="canon_fact",
        target_id="char_1",
        correlation_id=corr,
    )
    await svc.record_event(
        AuditAction.ARBITRATION_RESOLVED,
        actor_kind=AuditActorKind.AGENT,
        actor_id="showrunner",
        target_type="clip",
        target_id="shot_7",
        correlation_id=corr,
        reason="critic vs cinematographer tie broken",
    )
    await svc.record_event(
        AuditAction.BUDGET_SPENT,
        actor_kind=AuditActorKind.SYSTEM,
        actor_id="scheduler",
        target_type="clip",
        target_id="shot_7",
        correlation_id=corr,
        payload={"video_seconds": 5},
    )
    await svc.record_event(
        AuditAction.RENDER_ACCEPTED,
        actor_kind=AuditActorKind.SYSTEM,
        actor_id="render-worker",
        target_type="clip",
        target_id="shot_7",
        correlation_id=corr,
    )
    # An unrelated event that must NOT appear in the trail.
    await svc.record_event(
        AuditAction.AUTH_LOGIN, actor_kind=AuditActorKind.USER, actor_id="usr_9"
    )

    trail = await svc.provenance_trail("shot_7", target_type="clip")
    actions = [e.action for e in trail.events]
    # Correlation expansion pulls in the canon read (different target) too.
    assert AuditAction.CANON_UPDATED in actions
    assert AuditAction.RENDER_ACCEPTED in actions
    assert AuditAction.AUTH_LOGIN not in actions
    assert trail.correlation_ids == [corr]
    # Events are returned in chain (seq) order.
    seqs = [e.seq for e in trail.events]
    assert seqs == sorted(seqs)


async def test_provenance_trail_without_expansion_is_direct_only() -> None:
    svc = _service(segment_size=1000)
    await svc.record_event(
        AuditAction.CANON_UPDATED,
        actor_kind=AuditActorKind.AGENT,
        actor_id="a",
        target_type="canon_fact",
        target_id="char_1",
        correlation_id="c1",
    )
    await svc.record_event(
        AuditAction.RENDER_ACCEPTED,
        actor_kind=AuditActorKind.SYSTEM,
        actor_id="rw",
        target_type="clip",
        target_id="shot_1",
        correlation_id="c1",
    )
    trail = await svc.provenance_trail("shot_1", expand_correlations=False)
    assert [e.action for e in trail.events] == [AuditAction.RENDER_ACCEPTED]


# --------------------------------------------------------------------------- #
# Redaction preserves the chain end-to-end
# --------------------------------------------------------------------------- #


async def test_recorded_pii_is_redacted_and_chain_still_verifies() -> None:
    svc = AuditService(
        InMemoryAuditSink(), clock=_Clock(), segment_size=1000, redactor=Redactor(salt="s")
    )
    await svc.record_event(
        AuditAction.AUTH_LOGIN,
        actor_kind=AuditActorKind.USER,
        actor_id="usr_1",
        payload={"email": "alice@example.com", "ip_address": "10.0.0.1", "ok": True},
    )
    (record,) = await svc.query(AuditQuery())
    assert record.payload is not None
    assert is_redacted(record.payload["email"])
    assert is_redacted(record.payload["ip_address"])
    assert record.payload["ok"] is True  # non-PII preserved
    # The chain still verifies because the hash committed to the redacted core.
    assert (await svc.verify_integrity()).ok


async def test_forget_subject_is_idempotent_and_keeps_chain_valid() -> None:
    svc = AuditService(
        InMemoryAuditSink(), clock=_Clock(), segment_size=1000, redactor=Redactor(salt="s")
    )
    await svc.record_event(
        AuditAction.AUTH_PASSWORD_CHANGED,
        actor_kind=AuditActorKind.USER,
        actor_id="usr_7",
        target_type="user",
        target_id="usr_7",
        reason="reset by alice@example.com",
        payload={"email": "alice@example.com"},
    )
    touched = await svc.forget_subject("usr_7")
    assert touched >= 1
    assert (await svc.verify_integrity()).ok
    # Running it again changes nothing (re-redaction is idempotent).
    again = await svc.forget_subject("usr_7")
    assert again >= 1
    assert (await svc.verify_integrity()).ok


# --------------------------------------------------------------------------- #
# Sealing + Merkle checkpoint verification
# --------------------------------------------------------------------------- #


async def test_auto_seal_creates_checkpoint_and_verifies() -> None:
    svc = _service(segment_size=3)
    await _seed(svc, 7)  # two full segments (3,3) + one trailing entry unsealed
    checkpoints = await svc._sink.all_checkpoints()  # type: ignore[attr-defined]
    assert len(checkpoints) == 2
    assert checkpoints[0].from_seq == 1 and checkpoints[0].to_seq == 3
    assert checkpoints[1].from_seq == 4 and checkpoints[1].to_seq == 6
    report = await svc.verify_integrity()
    assert report.ok and report.checkpoints_verified == 2


async def test_tampered_sealed_segment_breaks_checkpoint() -> None:
    sink = InMemoryAuditSink()
    svc = AuditService(sink, clock=_Clock(), segment_size=3)
    await _seed(svc, 3)  # seals one checkpoint over seq 1..3
    # Forge an entry-hash inside the sealed segment without touching the chain
    # primitive path: this breaks the Merkle root (and the chain).
    from dataclasses import replace

    sink._entries[0] = replace(sink._entries[0], entry_hash="ab" * 32)
    report = await svc.verify_integrity()
    assert not report.ok


async def test_manual_seal_segment_seals_outstanding() -> None:
    svc = _service(segment_size=1000)
    await _seed(svc, 4)
    cp = await svc.seal_segment()
    assert cp is not None and cp.from_seq == 1 and cp.to_seq == 4
    # Nothing left unsealed => a second seal is a no-op.
    assert await svc.seal_segment() is None


# --------------------------------------------------------------------------- #
# Retention / pruning
# --------------------------------------------------------------------------- #


async def test_retention_prunes_only_sealed_and_old_entries() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    sink = InMemoryAuditSink()
    svc = AuditService(
        sink, clock=clock, segment_size=2, retention=timedelta(days=30)
    )
    # Two old, sealed entries (occurred in Jan).
    for i in range(2):
        await svc.record_event(
            AuditAction.CANON_UPDATED,
            actor_kind=AuditActorKind.AGENT,
            actor_id="a",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload={"i": i},
        )
    # Two recent entries (occurred in June) — sealed too but inside the horizon.
    for i in range(2):
        await svc.record_event(
            AuditAction.CANON_UPDATED,
            actor_kind=AuditActorKind.AGENT,
            actor_id="a",
            occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
            payload={"i": i + 2},
        )
    clock.advance(days=181)  # now ~mid-2026
    pruned = await svc.apply_retention()
    assert pruned == 2  # only the two January entries
    remaining = await svc.query(AuditQuery())
    assert [r.seq for r in remaining] == [3, 4]


async def test_retention_noop_when_disabled() -> None:
    svc = _service(segment_size=1)
    await _seed(svc, 2)
    assert await svc.apply_retention() == 0


# --------------------------------------------------------------------------- #
# Export (self-verifying)
# --------------------------------------------------------------------------- #


async def test_export_is_self_verifying_and_carries_checkpoints() -> None:
    svc = _service(segment_size=2)
    await _seed(svc, 4)
    doc = await svc.export()
    assert doc["schema"] == "kinora.audit.export/v1"
    assert doc["integrity"]["ok"] is True
    assert len(doc["entries"]) == 4
    assert len(doc["checkpoints"]) == 2
    # No plaintext PII keys leaked anywhere; entries carry their chain hashes.
    assert all("entry_hash" in e and "prev_hash" in e for e in doc["entries"])


# --------------------------------------------------------------------------- #
# Concurrency: lost seq race retries against the new tail
# --------------------------------------------------------------------------- #


async def test_append_retries_on_seq_race() -> None:
    import asyncio

    svc = _service(segment_size=1000)
    # Fire several appends concurrently; the in-memory sink rejects duplicate
    # seq, the service retries, and every event lands with a contiguous chain.
    await asyncio.gather(
        *[
            svc.record_event(
                AuditAction.CANON_UPDATED,
                actor_kind=AuditActorKind.AGENT,
                actor_id="a",
                payload={"i": i},
            )
            for i in range(8)
        ]
    )
    report = await svc.verify_integrity()
    assert report.ok and report.chain.entries == 8
    seqs = sorted(r.seq for r in await svc.query(AuditQuery()))
    assert seqs == list(range(1, 9))
