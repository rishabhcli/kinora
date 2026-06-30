"""Tamper-evident audit log + provenance system for Kinora (``app.audit``).

A structured, hash-chained, redaction-aware account of every *consequential*
action across the product — canon mutations, arbitration decisions, render
accept/degrade, budget spend, auth/lockout, config/flag changes — so a clip, a
canon fact, or a session can be explained end-to-end for debugging, compliance,
and provenance.

Layers (all additive, self-contained under this namespace):

* :mod:`app.audit.taxonomy` — the closed actor/category/action/severity vocabulary;
* :mod:`app.audit.events` — the typed :class:`AuditEvent` input contract (pydantic v2);
* :mod:`app.audit.chain` — the pure hash-chain + Merkle-checkpoint primitives;
* :mod:`app.audit.redaction` — PII scrubbing that *commits to* (never stores) the
  value, so the chain survives erasure;
* :mod:`app.audit.store` — the pluggable :class:`AuditSink` + in-memory reference;
* :mod:`app.audit.query` — the declarative search vocabulary;
* :mod:`app.audit.service` — :class:`AuditService`: record / ``verify_integrity`` /
  query / provenance trail / seal / retention / export;
* :mod:`app.audit.db` + :mod:`app.audit.db_models` — the durable DB sink (two
  additive append-only tables; migration ``audit_0001``).

The DB layer is imported lazily (via :func:`__getattr__`) so the pure core is
usable with no database on the path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.audit.chain import (
    ChainCheck,
    MerkleStep,
    merkle_proof,
    merkle_root,
    recompute_chain,
    verify_merkle_proof,
)
from app.audit.events import AuditEvent
from app.audit.query import AuditQuery
from app.audit.redaction import Redactor
from app.audit.service import (
    AuditService,
    DuplicateSeqError,
    IntegrityReport,
    ProvenanceTrail,
)
from app.audit.store import (
    AuditRecord,
    AuditSink,
    CheckpointRecord,
    InMemoryAuditSink,
)
from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
)

if TYPE_CHECKING:
    from app.audit.db import DbAuditSink
    from app.audit.db_models import AuditCheckpoint, AuditLogEntry

__all__ = [
    "AuditAction",
    "AuditActorKind",
    "AuditCategory",
    "AuditCheckpoint",
    "AuditEvent",
    "AuditLogEntry",
    "AuditQuery",
    "AuditRecord",
    "AuditService",
    "AuditSeverity",
    "AuditSink",
    "ChainCheck",
    "CheckpointRecord",
    "DbAuditSink",
    "DuplicateSeqError",
    "InMemoryAuditSink",
    "IntegrityReport",
    "MerkleStep",
    "ProvenanceTrail",
    "Redactor",
    "merkle_proof",
    "merkle_root",
    "recompute_chain",
    "verify_merkle_proof",
]


def __getattr__(name: str) -> Any:  # PEP 562 — lazy DB imports
    """Resolve the DB-layer symbols on demand (keeps the core DB-free)."""
    if name == "DbAuditSink":
        from app.audit.db import DbAuditSink

        return DbAuditSink
    if name in {"AuditLogEntry", "AuditCheckpoint"}:
        from app.audit import db_models

        return getattr(db_models, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
