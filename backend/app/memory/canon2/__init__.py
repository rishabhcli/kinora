"""canon2 — the deepened MCP canon memory (kinora.md §8, §7.2, §9.5).

A self-contained, additive layer over the existing canon memory that adds, under
its own ``canon2.*`` MCP-tool namespace:

* **versioned entities** — an append-only revision log per entity with field-level
  diffs, who/what/when provenance, and time-travel reads ("the canon as of page
  N" / "as the canon believed it at time T"): :mod:`.versioning`;
* **§7.2 conflict resolution** — a deterministic merge policy (grounded-wins →
  evolve, user-facing/ambiguous → flag, else last-writer-wins) with a
  flagged-conflict queue for arbitration: :mod:`.conflict`;
* **hybrid retrieval** — keyword+vector recall over canon facts with a pluggable
  embedder, MMR re-rank, and near-duplicate dedup: :mod:`.retrieval`;
* **consistency auditing** — drift + contradiction + dangling-reference detection
  across the accumulated canon: :mod:`.audit`;
* an in-memory store + a :class:`~app.memory.canon2.tools.Canon2Tools` dispatcher
  that mirrors :meth:`app.mcp.tools.MemoryTools.dispatch` and can be *mounted onto*
  the existing dispatch without changing any existing tool: :mod:`.store`,
  :mod:`.tools`.

Everything is pure / in-memory and deterministic — no DB, no network, no live
embeddings — so the whole subsystem is unit-testable offline.
"""

from __future__ import annotations

from app.memory.canon2.audit import (
    AuditReport,
    ConsistencyAuditor,
    Finding,
    Severity,
)
from app.memory.canon2.conflict import (
    ConflictPolicy,
    FlaggedConflict,
    Proposal,
    Resolution,
    build_options,
    resolve,
)
from app.memory.canon2.retrieval import CanonFact, CanonRetriever, RetrievedFact
from app.memory.canon2.store import Canon2Store
from app.memory.canon2.tools import (
    CANON2_TOOL_DEFS,
    CANON2_TOOLS_BY_NAME,
    Canon2ToolDef,
    Canon2Tools,
    MergedDispatcher,
    mount_on,
)
from app.memory.canon2.versioning import (
    Canon2Kind,
    EntityHistory,
    FieldDelta,
    Provenance,
    Revision,
    diff_attributes,
    revision_as_of_beat,
    revision_as_of_tx,
)

__all__ = [
    "CANON2_TOOLS_BY_NAME",
    "CANON2_TOOL_DEFS",
    "AuditReport",
    "Canon2Kind",
    "Canon2Store",
    "Canon2ToolDef",
    "Canon2Tools",
    "CanonFact",
    "CanonRetriever",
    "ConflictPolicy",
    "ConsistencyAuditor",
    "EntityHistory",
    "FieldDelta",
    "Finding",
    "FlaggedConflict",
    "MergedDispatcher",
    "Proposal",
    "Provenance",
    "Resolution",
    "RetrievedFact",
    "Revision",
    "Severity",
    "build_options",
    "diff_attributes",
    "mount_on",
    "resolve",
    "revision_as_of_beat",
    "revision_as_of_tx",
]
