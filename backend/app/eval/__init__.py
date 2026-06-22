"""The Track-3 metrics & evaluation harness (kinora.md §13).

The "measurable efficiency gain over single-agent baselines" proof:

* :mod:`app.eval.metrics` — the pure §13 metric math (CCS, accepted-footage
  efficiency, regeneration rate, style drift, latency-to-first-frame, buffer
  health);
* :mod:`app.eval.baseline` — the single-agent, no-memory, frame-chaining control
  arm;
* :mod:`app.eval.buffer_trace` — the real scheduler driven over a simulated read
  to produce the §4.10 watermark sawtooth, spending zero video-seconds;
* :mod:`app.eval.harness` — the §13 protocol (fixed sequence/seeds/prompts, crew
  vs baseline, mean+spread over N runs, pre-registered thresholds) → a report;
* :mod:`app.eval.run` — the ``python -m app.eval.run --book <id>`` CLI.
"""

from __future__ import annotations

from app.eval.baseline import BaselineArm
from app.eval.buffer_trace import BufferTraceResult, simulate_buffer_trace
from app.eval.harness import (
    PRE_REGISTERED,
    Arm,
    CrewArm,
    DemoSequence,
    DemoShot,
    EvalReport,
    PreRegisteredThresholds,
    SequenceRun,
    ShotOutcome,
    embed_locked_refs,
    run_protocol,
    score_run,
)
from app.eval.metrics import (
    BufferHealth,
    BufferSample,
    LatencyToFirstFrame,
    accepted_footage_efficiency,
    ccs_from_embeddings,
    character_consistency_score,
    latency_to_first_frame,
    regeneration_rate,
    style_drift,
)

__all__ = [
    "PRE_REGISTERED",
    "Arm",
    "BaselineArm",
    "BufferHealth",
    "BufferSample",
    "BufferTraceResult",
    "CrewArm",
    "DemoSequence",
    "DemoShot",
    "EvalReport",
    "LatencyToFirstFrame",
    "PreRegisteredThresholds",
    "SequenceRun",
    "ShotOutcome",
    "accepted_footage_efficiency",
    "ccs_from_embeddings",
    "character_consistency_score",
    "embed_locked_refs",
    "latency_to_first_frame",
    "regeneration_rate",
    "run_protocol",
    "score_run",
    "simulate_buffer_trace",
    "style_drift",
]
