"""HTTP/SSE/WebSocket transport — the Kinora API gateway (kinora.md §5.6, §9.1).

The gateway is the thin edge over the :class:`app.composition.Container`: JWT
auth, validated PDF upload that triggers Phase A ingest, the session /
intent / seek control surface that drives the Scheduler, the Director tools
(region comment routing + surgical canon-edit regen), and the SSE + WebSocket
event channel that fans out every §5.6 generation event to the client.
"""

from __future__ import annotations
