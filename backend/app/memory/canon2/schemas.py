"""Typed input/output models for the canon2 MCP tools (kinora.md §8.3 style).

Mirrors the flat-input convention of :mod:`app.mcp.schemas`: inputs are scalars +
lists + plain JSON objects so the advertised JSON Schema reads cleanly as a Qwen
function-call parameter set; outputs reuse the canon2 domain contracts so the tool
surface and the engine speak the same types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.memory.canon2.audit import AuditReport
from app.memory.canon2.conflict import FlaggedConflict, Resolution
from app.memory.canon2.retrieval import RetrievedFact
from app.memory.canon2.versioning import Canon2Kind, EntityHistory, Revision

# --------------------------------------------------------------------------- #
# canon2.upsert_entity / get_entity / history / diff
# --------------------------------------------------------------------------- #


class Canon2UpsertEntityInput(BaseModel):
    book_id: str
    entity_key: str
    type: Canon2Kind
    name: str
    valid_from_beat: int
    branch: str = "main"
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    appearance: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    style_tokens: dict[str, Any] | None = None
    actor_id: str = "system"
    reason: str | None = None
    source_span: dict[str, Any] | None = None
    proposed_by: str | None = None


class Canon2GetEntityInput(BaseModel):
    book_id: str
    entity_key: str
    branch: str = "main"
    at_beat: int | None = None
    as_of_tx: datetime | None = None


class Canon2GetEntityOutput(BaseModel):
    found: bool
    revision: Revision | None = None


class Canon2HistoryInput(BaseModel):
    book_id: str
    entity_key: str
    branch: str = "main"


class Canon2HistoryOutput(BaseModel):
    found: bool
    history: EntityHistory | None = None


# --------------------------------------------------------------------------- #
# canon2.propose_fact / conflicts / resolve_conflict
# --------------------------------------------------------------------------- #


class Canon2ProposeFactInput(BaseModel):
    book_id: str
    subject: str
    predicate: str
    object_value: str
    branch: str = "main"
    valid_from_beat: int = 0
    current_beat: int | None = None
    actor_id: str = "system"
    wall_ms: int = 0
    counter: int = 0
    source_span: dict[str, Any] | None = None
    user_directed: bool = False
    reason: str | None = None


class Canon2ConflictsInput(BaseModel):
    book_id: str
    branch: str = "main"
    include_resolved: bool = False


class Canon2ConflictsOutput(BaseModel):
    conflicts: list[FlaggedConflict] = Field(default_factory=list)


class Canon2ResolveConflictInput(BaseModel):
    book_id: str
    conflict_id: str
    chosen_object: str
    branch: str = "main"
    resolved_by: str = "director"
    reasoning: str | None = None
    valid_from_beat: int = 0


# --------------------------------------------------------------------------- #
# canon2.retrieve
# --------------------------------------------------------------------------- #


class Canon2RetrieveInput(BaseModel):
    book_id: str
    query: str
    branch: str = "main"
    k: int = 5
    lambda_: float = 0.6


class Canon2RetrieveOutput(BaseModel):
    results: list[RetrievedFact] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# canon2.audit
# --------------------------------------------------------------------------- #


class Canon2AuditInput(BaseModel):
    book_id: str
    branch: str = "main"
    #: Pairs of predicates declared incompatible (e.g. ["alive", "dead"]).
    mutually_exclusive: list[tuple[str, str]] = Field(default_factory=list)


__all__ = [
    "AuditReport",
    "Canon2AuditInput",
    "Canon2ConflictsInput",
    "Canon2ConflictsOutput",
    "Canon2GetEntityInput",
    "Canon2GetEntityOutput",
    "Canon2HistoryInput",
    "Canon2HistoryOutput",
    "Canon2ProposeFactInput",
    "Canon2ResolveConflictInput",
    "Canon2RetrieveInput",
    "Canon2RetrieveOutput",
    "Canon2UpsertEntityInput",
    "FlaggedConflict",
    "Resolution",
    "Revision",
]
