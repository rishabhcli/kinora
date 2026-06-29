"""In-memory broker implementation — the zero-infra test double.

:class:`~app.streaming.log.memory.broker.InMemoryBroker` implements the full
:class:`~app.streaming.log.broker.Broker` protocol in plain async Python: real
partition logs (offsets/retention/compaction), an embedded group coordinator,
per-producer idempotence sequencing + epoch fencing, and a transaction buffer
for exactly-once semantics. It is the broker the whole substrate's unit tests run
against — and a perfectly good default for single-process Kinora deployments.
"""

from __future__ import annotations

from app.streaming.log.memory.broker import InMemoryBroker

__all__ = ["InMemoryBroker"]
