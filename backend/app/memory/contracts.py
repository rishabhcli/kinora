"""JSON-serializable contracts for the bitemporal canon engine (kinora.md §8).

These mirror :mod:`app.memory.interfaces` (the existing ``CanonSlice`` family): they are the
typed boundary the new MCP tools speak and the frontend's inspectable read contract. They
carry both time axes — valid-time as integer beats, transaction-time as ISO-8601 UTC — plus
the branch and CRDT stamp so the wire form is fully self-describing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BeatSpan(BaseModel):
    """A valid-time beat interval ``[from, to)`` on the story timeline (§8.5)."""

    valid_from_beat: int
    valid_to_beat: int | None = None


class TxSpan(BaseModel):
    """A transaction-time interval ``[from, to)`` (UTC) — when the system believed a fact."""

    tx_from: datetime
    tx_to: datetime | None = None


class WriteStamp(BaseModel):
    """A CRDT write-stamp: the Hybrid-Logical-Clock tick + the writing actor."""

    wall: int
    counter: int
    actor_id: str


class BitemporalFact(BaseModel):
    """One belief of a continuity fact, fully pinned in (valid, tx, branch) space."""

    id: str
    fact_key: str
    branch: str = "main"
    subject_entity_key: str
    predicate: str
    object_value: str
    valid: BeatSpan
    tx: TxSpan
    stamp: WriteStamp
    source_span: dict[str, Any] | None = None
    #: True iff this row is the current belief on its branch (``tx_to`` open).
    current: bool = True


class FactHistory(BaseModel):
    """The full transaction-time history of one logical fact (every past belief)."""

    fact_key: str
    book_id: str
    branch: str
    beliefs: list[BitemporalFact] = Field(default_factory=list)


class AuditEntry(BaseModel):
    """One immutable, hash-chained canon-audit row (§8, tamper-evident)."""

    id: str
    seq: int
    book_id: str
    branch: str
    action: str
    actor_id: str
    target_key: str | None = None
    payload: dict[str, Any] | None = None
    prev_hash: str | None = None
    entry_hash: str
    created_at: datetime | None = None


class AuditChain(BaseModel):
    """A replayed audit log + whether its hash-chain verifies end-to-end."""

    book_id: str
    entries: list[AuditEntry] = Field(default_factory=list)
    intact: bool = True
    #: When ``intact`` is False, the ``seq`` of the first row that failed verification.
    broken_at_seq: int | None = None


class BranchInfo(BaseModel):
    """A canon branch in the registry (FORK / DIFF / MERGE)."""

    id: str
    book_id: str
    name: str
    parent: str | None = None
    status: str = "open"
    base_beat: int | None = None
    base_tx: datetime | None = None
    note: str | None = None


class FactChange(BaseModel):
    """One entry in a branch diff — how a fact differs between two branches."""

    fact_key: str
    subject_entity_key: str
    predicate: str
    #: "added" | "removed" | "changed" | "retired"
    change: str
    object_before: str | None = None
    object_after: str | None = None


class BranchDiff(BaseModel):
    """The structural difference between two branches' current beliefs."""

    book_id: str
    branch_a: str
    branch_b: str
    changes: list[FactChange] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes


class MergeConflict(BaseModel):
    """A genuinely-concurrent edit the merge resolved by the CRDT rule (LWW)."""

    fact_key: str
    subject_entity_key: str
    predicate: str
    source_object: str
    target_object: str
    winner: str  # "source" | "target"
    reason: str


class MergeResult(BaseModel):
    """The outcome of merging ``source`` into ``target`` (§8 + §7.2)."""

    book_id: str
    source: str
    target: str
    #: "fast_forward" | "merged" | "no_op"
    strategy: str
    applied: int = 0
    conflicts: list[MergeConflict] = Field(default_factory=list)
    merged_facts: list[str] = Field(default_factory=list)


class CanonReadView(BaseModel):
    """The clean, inspectable read contract the frontend canon editor consumes.

    A 4-D snapshot: every active fact on a branch at a beat (and optionally as of a past tx),
    plus the branch metadata and a tail of the audit log so "what changed and who changed it"
    is visible without a second round-trip.
    """

    book_id: str
    branch: str
    beat: int
    as_of_tx: datetime | None = None
    facts: list[BitemporalFact] = Field(default_factory=list)
    branches: list[BranchInfo] = Field(default_factory=list)
    audit_tail: list[AuditEntry] = Field(default_factory=list)


__all__ = [
    "AuditChain",
    "AuditEntry",
    "BeatSpan",
    "BitemporalFact",
    "BranchDiff",
    "BranchInfo",
    "CanonReadView",
    "FactChange",
    "FactHistory",
    "MergeConflict",
    "MergeResult",
    "TxSpan",
    "WriteStamp",
]
