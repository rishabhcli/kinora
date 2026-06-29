"""Concrete Kinora sagas — the two multi-step flows modelled as compensatable sagas.

This subpackage turns two of Kinora's real cross-service flows into saga
definitions the engine can drive durably:

* :mod:`~app.distributed.sagas.flows.ingest` — the **ingest → canon-build →
  identity-lock** flow: import a PDF, extract pages, build the canon graph, lock
  character identity references, and mark the book ready. Each step has a
  compensation so a failure half-way rolls back cleanly (delete the partial canon,
  release reserved object storage, revert the book to ``failed``).
* :mod:`~app.distributed.sagas.flows.render` — the **render → QA → conflict →
  degrade** flow (kinora.md §9.7 per-shot state machine + §7.2 arbitration): reserve
  budget, render the clip, run the Critic's QA, and on a canon conflict apply the
  Showrunner arbitration policy, degrading to the Ken-Burns ladder when retries are
  exhausted. Compensations release the budget reservation and the cache slot.

Both flows depend only on small **service ports** (Protocols) so they are wired to
the real backend services in production and to in-memory **fakes** in tests — zero
credits, ``KINORA_LIVE_VIDEO`` irrelevant, fully deterministic under the manual
clock. The ports live next to each flow; the fakes live in
:mod:`~app.distributed.sagas.flows.fakes`.
"""

from __future__ import annotations

from app.distributed.sagas.flows.ingest import (
    IngestPorts,
    build_ingest_saga,
)
from app.distributed.sagas.flows.render import (
    ArbitrationDecision,
    RenderPorts,
    build_render_saga,
)

__all__ = [
    "ArbitrationDecision",
    "IngestPorts",
    "RenderPorts",
    "build_ingest_saga",
    "build_render_saga",
]
