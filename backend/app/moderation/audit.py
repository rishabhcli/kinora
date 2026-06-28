"""The immutable, hash-chained moderation audit log (§9/§10, §8-style).

Every consequential moderation action — a gate decision, a review transition, a
takedown, an appeal, a policy change, an escalation — is recorded as one
**immutable** row whose ``entry_hash`` covers the *previous* row's hash plus this
row's payload. Re-hashing the chain detects any retroactive edit, so the log is
**tamper-evident**: a regulator or auditor can prove the moderation history was
not silently rewritten.

The chain is **per-tenant** (a tenant's ``seq`` is monotone and independent), so
one tenant's history replays and verifies on its own. This mirrors the canon
audit log (:mod:`app.memory.audit_log`) but is scoped to the moderation domain.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.moderation.models import ModerationAuditEntry
from app.moderation.repositories import ModerationAuditRepo


class AuditAction(enum.StrEnum):
    """The kinds of moderation mutation the audit log records."""

    SCREEN = "screen"  # a gate decision (allow/flag/block)
    ENQUEUE_REVIEW = "enqueue_review"
    CLAIM_REVIEW = "claim_review"
    APPROVE = "approve"
    REJECT = "reject"
    TAKEDOWN = "takedown"
    APPEAL = "appeal"
    APPEAL_GRANT = "appeal_grant"
    APPEAL_DENY = "appeal_deny"
    ESCALATE = "escalate"
    POLICY_CHANGE = "policy_change"
    SUSPEND = "suspend"
    REINSTATE = "reinstate"


def _canonical(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class AuditEntryView(BaseModel):
    """A read-model projection of one audit entry."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    seq: int
    action: str
    actor_id: str
    target_id: str | None
    payload: dict[str, Any] | None
    prev_hash: str | None
    entry_hash: str
    created_at: datetime


class AuditChainView(BaseModel):
    """The replayed chain plus its verification result."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    entries: list[AuditEntryView]
    intact: bool
    broken_at_seq: int | None = None


class ModerationAuditLog:
    """Append + replay + verify the tamper-evident moderation audit log."""

    def __init__(self, repo: ModerationAuditRepo) -> None:
        self._repo = repo

    async def record(
        self,
        *,
        tenant_id: str,
        action: AuditAction,
        actor_id: str,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEntryView:
        """Append one mutation to the tenant's chain."""
        row = await self._repo.append(
            tenant_id=tenant_id,
            action=action.value,
            actor_id=actor_id,
            target_id=target_id,
            payload=payload,
        )
        return _to_view(row)

    async def replay(self, tenant_id: str, *, limit: int | None = None) -> AuditChainView:
        """Replay the tenant's log and verify its hash-chain end-to-end.

        Verification always runs over the **full** chain (so a tampered early row
        is caught even when only the tail is returned); ``limit`` only trims the
        returned entries.
        """
        full = await self._repo.replay(tenant_id, limit=None)
        intact, broken = self._verify(full)
        entries = [_to_view(r) for r in full]
        if limit is not None:
            entries = entries[-limit:]
        return AuditChainView(
            tenant_id=tenant_id, entries=entries, intact=intact, broken_at_seq=broken
        )

    @staticmethod
    def _verify(rows: list[ModerationAuditEntry]) -> tuple[bool, int | None]:
        prev_hash: str | None = None
        for row in rows:
            expected = ModerationAuditRepo.compute_hash(
                prev_hash,
                seq=row.seq,
                action=row.action,
                actor_id=row.actor_id,
                target_id=row.target_id,
                payload_repr=_canonical(row.payload),
            )
            if expected != row.entry_hash or (row.prev_hash or None) != (prev_hash or None):
                return False, row.seq
            prev_hash = row.entry_hash
        return True, None


def _to_view(row: ModerationAuditEntry) -> AuditEntryView:
    return AuditEntryView(
        id=row.id,
        tenant_id=row.tenant_id,
        seq=row.seq,
        action=row.action,
        actor_id=row.actor_id,
        target_id=row.target_id,
        payload=row.payload,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        created_at=row.created_at,
    )


__all__ = ["AuditAction", "AuditChainView", "AuditEntryView", "ModerationAuditLog"]
