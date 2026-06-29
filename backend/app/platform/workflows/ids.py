"""Opaque id generation for engine-internal records.

Workflow ids are *caller-chosen* (they're the dedup/uniqueness key for an
execution, like ``ingest:book_42``); everything else the engine creates — run
ids, task ids, timer ids — gets an opaque random id with a short type prefix so
logs are greppable (``act_…`` for an activity task, ``wft_…`` for a workflow
task). These ids are *not* part of the deterministic replay surface (workflow
code never sees them), so a random source is fine and correct here.
"""

from __future__ import annotations

import uuid


def new_id(prefix: str = "id") -> str:
    """A fresh opaque id with a short type ``prefix`` (e.g. ``act_3f9a…``)."""
    return f"{prefix}_{uuid.uuid4().hex}"


__all__ = ["new_id"]
