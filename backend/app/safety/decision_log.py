"""The immutable, hash-chained safety DECISION LOG with appeal/override hooks.

Every consequential gateway decision — a prompt verdict, an output quarantine, an
operator override, an appeal grant/deny, an advisory tagging — is appended as one
**immutable** record whose ``this_hash`` covers the *previous* record's hash plus
this record's canonical payload. Re-hashing the chain detects any retroactive
edit, so the log is **tamper-evident**: an auditor can prove the safety history
was not silently rewritten. The chain is **per-tenant** (each tenant's ``seq`` is
monotone and independent), mirroring :mod:`app.moderation.audit`.

Two layers, behind the :class:`DecisionLog` Protocol:

* :class:`InMemoryDecisionLog` — the deterministic, no-DB default used in tests
  and offline. Hash chain, append-only, appeal/override transitions, verify/replay.
* A DB-backed implementation can satisfy the same Protocol in production; the
  gateway only depends on the Protocol so wiring is additive.

Appeal / override semantics
---------------------------
A recorded decision is **never mutated**. An appeal or override is itself a new
appended record that *references* the original by id; querying a decision's
effective state walks the chain forward to the latest appeal/override record for
it. This keeps the chain monotone and the original verdict permanently inspectable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.safety.contracts import (
    AppealState,
    DecisionKind,
    DecisionRecordView,
    OutputAssessment,
    PromptDecision,
    SafetyCategory,
    SafetyContext,
    Severity,
)

#: The genesis previous-hash for the first record in a tenant chain.
GENESIS_HASH = "0" * 64


def _canonical(payload: Mapping[str, Any] | None) -> str:
    """Stable JSON for hashing — sorted keys, no whitespace, str-coerced."""
    if not payload:
        return ""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(
    *,
    prev_hash: str,
    seq: int,
    tenant_id: str,
    kind: DecisionKind,
    action: str,
    payload: Mapping[str, Any] | None,
) -> str:
    """SHA-256 over the previous hash + this record's identity + payload.

    Pure and deterministic: the same inputs always yield the same hash, so the
    chain replays identically and a single byte's change anywhere downstream of a
    record breaks every subsequent hash.
    """
    material = "|".join(
        [
            prev_hash,
            str(seq),
            tenant_id,
            str(kind),
            action,
            _canonical(payload),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _new_id(prefix: str, seq: int, tenant_id: str) -> str:
    """A deterministic record id (no RNG ⇒ reproducible tests)."""
    digest = hashlib.sha256(f"{tenant_id}:{seq}".encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class DecisionRecord:
    """One immutable decision-log record (a single link in the hash chain).

    Frozen — once appended it never changes; an appeal/override is a *new* record
    that references this one by :attr:`id`.
    """

    seq: int
    id: str
    kind: DecisionKind
    tenant_id: str
    action: str
    severity: Severity
    categories: tuple[SafetyCategory, ...]
    reason: str
    surface: str | None
    book_id: str | None
    shot_id: str | None
    correlation_id: str | None
    appeal_state: AppealState
    references: str | None  # id of the record this one appeals/overrides
    payload: dict[str, Any]
    prev_hash: str
    this_hash: str
    created_at: datetime

    def to_view(self) -> DecisionRecordView:
        from app.safety.contracts import SafetySurface

        surface = None
        if self.surface is not None:
            try:
                surface = SafetySurface(self.surface)
            except ValueError:
                surface = None
        return DecisionRecordView(
            seq=self.seq,
            id=self.id,
            kind=self.kind,
            tenant_id=self.tenant_id,
            surface=surface,
            action=self.action,
            severity=self.severity,
            categories=list(self.categories),
            reason=self.reason,
            book_id=self.book_id,
            shot_id=self.shot_id,
            appeal_state=self.appeal_state,
            prev_hash=self.prev_hash,
            this_hash=self.this_hash,
            created_at=self.created_at,
            payload=self.payload or None,
        )


@dataclass(frozen=True)
class ChainVerification:
    """The result of replaying + verifying a tenant's chain."""

    tenant_id: str
    length: int
    intact: bool
    #: The seq of the first record whose hash does not recompute (None if intact).
    first_broken_seq: int | None = None


@runtime_checkable
class DecisionLog(Protocol):
    """The append-only, hash-chained decision log the gateway records to."""

    async def record_prompt(
        self, decision: PromptDecision, *, context: SafetyContext
    ) -> DecisionRecord: ...

    async def record_output(
        self, assessment: OutputAssessment, *, context: SafetyContext
    ) -> DecisionRecord: ...

    async def record_override(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        new_action: str,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord: ...

    async def request_appeal(
        self, *, record_id: str, context: SafetyContext, reason: str
    ) -> DecisionRecord: ...

    async def resolve_appeal(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        granted: bool,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord: ...

    async def history(self, tenant_id: str) -> list[DecisionRecord]: ...

    async def verify(self, tenant_id: str) -> ChainVerification: ...


@dataclass
class _Clock:
    """Injectable clock so timestamps are deterministic in tests."""

    _now: datetime | None = None

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)


class InMemoryDecisionLog:
    """Deterministic, no-DB decision log — the test/offline default.

    Per-tenant monotone ``seq`` + SHA-256 hash chain. Append-only; appeals and
    overrides are new records referencing the original. :meth:`verify` re-hashes a
    tenant's chain and reports the first broken link.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._chains: dict[str, list[DecisionRecord]] = {}
        self._by_id: dict[str, DecisionRecord] = {}
        self._clock = _Clock(_now=now)

    # -- append primitive ------------------------------------------------- #

    def _append(
        self,
        *,
        tenant_id: str,
        kind: DecisionKind,
        action: str,
        severity: Severity,
        categories: tuple[SafetyCategory, ...],
        reason: str,
        surface: str | None,
        context: SafetyContext,
        references: str | None,
        appeal_state: AppealState,
        payload: dict[str, Any],
    ) -> DecisionRecord:
        chain = self._chains.setdefault(tenant_id, [])
        seq = len(chain) + 1
        prev_hash = chain[-1].this_hash if chain else GENESIS_HASH
        this_hash = compute_hash(
            prev_hash=prev_hash,
            seq=seq,
            tenant_id=tenant_id,
            kind=kind,
            action=action,
            payload=payload,
        )
        record = DecisionRecord(
            seq=seq,
            id=_new_id(kind.value, seq, tenant_id),
            kind=kind,
            tenant_id=tenant_id,
            action=action,
            severity=severity,
            categories=categories,
            reason=reason,
            surface=surface,
            book_id=context.book_id,
            shot_id=context.shot_id,
            correlation_id=context.correlation_id,
            appeal_state=appeal_state,
            references=references,
            payload=payload,
            prev_hash=prev_hash,
            this_hash=this_hash,
            created_at=self._clock.now(),
        )
        chain.append(record)
        self._by_id[record.id] = record
        return record

    # -- public API ------------------------------------------------------- #

    async def record_prompt(
        self, decision: PromptDecision, *, context: SafetyContext
    ) -> DecisionRecord:
        payload: dict[str, Any] = {
            "surface": str(decision.surface),
            "action": str(decision.action),
            "severity": int(decision.severity),
            "categories": [c.value for c in decision.categories],
            "effective_prompt": decision.effective_prompt,
            "policy_version": decision.policy_version,
            "classifier": decision.classifier,
            "degraded": decision.degraded,
            "reason": decision.reason,
            "user_id": context.user_id,
        }
        if decision.softening is not None:
            payload["softening"] = {
                "changed": decision.softening.changed,
                "transforms": decision.softening.transforms,
                "unsoftenable": [c.value for c in decision.softening.unsoftenable],
            }
        if decision.routing is not None:
            payload["routing"] = {
                "providers": decision.routing.ordered_providers,
                "avoided": [c.value for c in decision.routing.avoided_categories],
            }
        return self._append(
            tenant_id=context.tenant_id,
            kind=DecisionKind.PROMPT,
            action=str(decision.action),
            severity=decision.severity,
            categories=tuple(decision.categories),
            reason=decision.reason,
            surface=str(decision.surface),
            context=context,
            references=None,
            appeal_state=AppealState.NONE,
            payload=payload,
        )

    async def record_output(
        self, assessment: OutputAssessment, *, context: SafetyContext
    ) -> DecisionRecord:
        payload = {
            "surface": str(assessment.surface),
            "verdict": str(assessment.verdict),
            "severity": int(assessment.severity),
            "categories": [c.value for c in assessment.categories],
            "sampled_frames": assessment.sampled_frames,
            "classifier": assessment.classifier,
            "degraded": assessment.degraded,
            "reason": assessment.reason,
            "user_id": context.user_id,
        }
        return self._append(
            tenant_id=context.tenant_id,
            kind=DecisionKind.OUTPUT,
            action=str(assessment.verdict),
            severity=assessment.severity,
            categories=tuple(assessment.categories),
            reason=assessment.reason,
            surface=str(assessment.surface),
            context=context,
            references=None,
            appeal_state=AppealState.NONE,
            payload=payload,
        )

    async def record_override(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        new_action: str,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord:
        original = self._require(record_id)
        payload = {
            "references": record_id,
            "original_action": original.action,
            "new_action": new_action,
            "actor_id": actor_id,
            "reason": reason,
        }
        return self._append(
            tenant_id=context.tenant_id,
            kind=DecisionKind.OVERRIDE,
            action=new_action,
            severity=original.severity,
            categories=original.categories,
            reason=reason,
            surface=original.surface,
            context=context,
            references=record_id,
            appeal_state=AppealState.NONE,
            payload=payload,
        )

    async def request_appeal(
        self, *, record_id: str, context: SafetyContext, reason: str
    ) -> DecisionRecord:
        original = self._require(record_id)
        payload = {"references": record_id, "reason": reason}
        return self._append(
            tenant_id=context.tenant_id,
            kind=DecisionKind.APPEAL,
            action="appeal_requested",
            severity=original.severity,
            categories=original.categories,
            reason=reason,
            surface=original.surface,
            context=context,
            references=record_id,
            appeal_state=AppealState.REQUESTED,
            payload=payload,
        )

    async def resolve_appeal(
        self,
        *,
        record_id: str,
        context: SafetyContext,
        granted: bool,
        actor_id: str,
        reason: str,
    ) -> DecisionRecord:
        original = self._require(record_id)
        state = AppealState.GRANTED if granted else AppealState.DENIED
        payload = {
            "references": record_id,
            "granted": granted,
            "actor_id": actor_id,
            "reason": reason,
        }
        return self._append(
            tenant_id=context.tenant_id,
            kind=DecisionKind.APPEAL,
            action=f"appeal_{state.value}",
            severity=original.severity,
            categories=original.categories,
            reason=reason,
            surface=original.surface,
            context=context,
            references=record_id,
            appeal_state=state,
            payload=payload,
        )

    async def history(self, tenant_id: str) -> list[DecisionRecord]:
        return list(self._chains.get(tenant_id, []))

    async def get(self, record_id: str) -> DecisionRecord | None:
        return self._by_id.get(record_id)

    async def effective_appeal_state(self, record_id: str) -> AppealState:
        """Walk forward to the latest appeal/override referencing ``record_id``."""
        original = self._by_id.get(record_id)
        if original is None:
            return AppealState.NONE
        state = AppealState.NONE
        for rec in self._chains.get(original.tenant_id, []):
            if rec.references == record_id and rec.appeal_state is not AppealState.NONE:
                state = rec.appeal_state
        return state

    async def verify(self, tenant_id: str) -> ChainVerification:
        chain = self._chains.get(tenant_id, [])
        prev = GENESIS_HASH
        for rec in chain:
            expected = compute_hash(
                prev_hash=prev,
                seq=rec.seq,
                tenant_id=rec.tenant_id,
                kind=rec.kind,
                action=rec.action,
                payload=rec.payload,
            )
            if expected != rec.this_hash or rec.prev_hash != prev:
                return ChainVerification(
                    tenant_id=tenant_id,
                    length=len(chain),
                    intact=False,
                    first_broken_seq=rec.seq,
                )
            prev = rec.this_hash
        return ChainVerification(tenant_id=tenant_id, length=len(chain), intact=True)

    def _require(self, record_id: str) -> DecisionRecord:
        rec = self._by_id.get(record_id)
        if rec is None:
            raise KeyError(f"unknown decision record: {record_id!r}")
        return rec


__all__ = [
    "GENESIS_HASH",
    "ChainVerification",
    "DecisionLog",
    "DecisionRecord",
    "InMemoryDecisionLog",
    "compute_hash",
]
