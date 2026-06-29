"""Concrete Kinora pipelines over the engine.

Two production-shaped dataflows, each a thin :class:`StreamEnvironment` topology
plus the event models its source carries:

* :mod:`events` — the wire models: :class:`ReaderIntentEvent` (the §4.3
  reading-position signal: ``focus_word`` / ``velocity_wps`` / ``mode`` /
  ``seek``) and :class:`RenderEvent` (the §5.6 generation events:
  ``keyframe_ready`` / ``clip_ready`` / ``regen_done`` / QA results).
* :mod:`engagement` — live engagement & velocity analytics over the
  reader-intent stream: per-session windowed reading velocity, dwell, scroll
  burst detection, stall detection, and session-window reading-session shaping.
* :mod:`render_qa` — render throughput & QA dashboards over the render-event
  stream: shots/min, accept rate, regeneration rate (§13), p50/p95
  render-latency via a request↔clip interval join, and budget burn-down.

Both are pure topology factories — they take a list of events and return an
:class:`~app.streaming.processing.runtime.ExecutionResult`, so they are trivially
testable with the deterministic driver and feed the §13 metrics panel.
"""

from __future__ import annotations
