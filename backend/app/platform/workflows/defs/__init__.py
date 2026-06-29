"""Concrete durable workflows for Kinora's long-running pipelines.

Two production-shaped orchestrations built on the engine, registered into the
shared :data:`~app.platform.workflows.registry.DEFAULT_WORKFLOW_REGISTRY` /
:data:`DEFAULT_ACTIVITY_REGISTRY` on import:

* :mod:`app.platform.workflows.defs.ingest_render` — **book-ingest → render-whole-
  scene** (kinora.md §9.7/§12): the durable choreography of Phase-A ingest
  (extract → analyze → canon-build → identity-lock), then fan-out of a scene's
  shots through the per-shot state machine (cache-check → render → QA → repair ≤2
  → degrade) with budget gating and a dead-letter-to-Ken-Burns lane. Survives
  crashes at any point because every stage transition is an event in history.
* :mod:`app.platform.workflows.defs.episode` — the multi-agent **"produce an
  episode"** orchestration: the six-agent crew (Adapter, Cinematographer, Critic,
  Continuity, Showrunner, Director) coordinated as a durable workflow with a
  director-approval **signal** gate, a live-progress **query**, child workflows
  per scene, and **continue-as-new** to keep the history compact across a
  book-length run.

The activities here are thin, idempotent adapters: in this build they are
self-contained simulations (zero credits, ``KINORA_LIVE_VIDEO`` OFF) so the
workflows are fully runnable and tested without infra; wiring them to the real
:mod:`app.ingest`/:mod:`app.render` services is a localized change inside each
activity body (documented in ``DESIGN.md``), leaving the durable orchestration —
the hard part — unchanged.
"""

from __future__ import annotations

from app.platform.workflows.defs import episode, ingest_render

__all__ = ["episode", "ingest_render"]
