"""Machine-checkable models of Kinora's concurrency-critical protocols.

Each module builds a :class:`~app.verification.modelcheck.spec.Spec` over a
*finite abstraction* of one real protocol and declares the invariants and
liveness properties the production code is supposed to satisfy. They are not
re-implementations of the backend — they are the *contracts* the backend's
concurrency must honour, expressed at the abstraction level where exhaustive
state-space exploration is feasible.

* :mod:`scheduler_buffer` — the §4.5–§4.9 dual-watermark promotion protocol:
  burst/idle hysteresis, velocity-adaptive promotion, idle-pause, seek/cancel.
  Invariants: the committed buffer never goes negative, reserved video-seconds
  never exceed the budget (no double-spend), the burst flag tracks the
  watermark band. Liveness: a drained buffer is eventually refilled; an idle
  reader's speculation eventually halts.

* :mod:`render_queue` — the §12.1 job lifecycle: queued → reserved → submitted →
  polling → succeeded / retrying / cancelled / dead-letter, with leases and a
  reaper. Invariants: a job is in at most one lane at a time, an acked job's
  budget is released exactly once, a cancellation is never lost. Liveness: every
  claimed job eventually reaches a terminal state (accepted-or-degraded), and a
  cancel request leads-to the job leaving the active set.

* :mod:`arbitration` — the §7.2 conflict resolution policy: drafted → checked →
  approved / conflict → arbitration → {honor, evolve, surface} → approved.
  Invariants: evolve_canon only with textual support, surface only with a
  director present on a user-facing conflict, every conflict resolves to a
  logged decision. Liveness: every raised conflict leads-to an approved shot.

Every spec is parametrised by small bounds (buffer slots, job count, worker
count) so the reachable state space stays enumerable; the bounds are chosen
large enough to exercise the interesting interleavings (preemption, concurrent
claims, a seek mid-render) and small enough to finish in well under a second.
"""

from __future__ import annotations

from app.verification.specs.arbitration import build_arbitration_spec
from app.verification.specs.fairness import build_fairness_spec, session_symmetry
from app.verification.specs.render_queue import build_render_queue_spec
from app.verification.specs.scheduler_buffer import build_scheduler_buffer_spec

__all__ = [
    "build_arbitration_spec",
    "build_fairness_spec",
    "build_render_queue_spec",
    "build_scheduler_buffer_spec",
    "session_symmetry",
]
