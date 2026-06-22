"""Empirical proof of the watermark sawtooth (§4.5/§4.10) — with ZERO video.

This drives the **real** :class:`~app.scheduler.service.SchedulerService` control
loop (the real zones/ETA math, the real dual-watermark hysteresis, the real
velocity-adaptive promotion) over a *simulated* reader who advances the focus
word at a fixed velocity across a book's source-span index, sampling
``committed_seconds_ahead`` at every tick. The result is the §4.10 sawtooth:
fill to the high watermark ``H``, idle in the ``[L, H)`` band, burst-refill once
the buffer drains below the low watermark ``L``.

Crucially it spends **zero video-seconds**:

* the budget gate is a :class:`DryRunBudget` — it lets promotion proceed (so the
  committed sawtooth forms, driven by each shot's ``est_duration_s``) but reserves
  **0.0 actual video-seconds** and is never charged;
* the queue is a :class:`RecordingQueue` that records enqueues but renders
  nothing — no Wan call, no worker;
* the keyframe lane is a :class:`RecordingKeyframes` (image-gen would be on the
  cheap, zero-video lane anyway, §4.4).

So ``video_seconds_spent`` and ``video_reservations_s`` are both ``0.0`` by
construction — exactly what the §13 buffer-health proof claims. This is also what
powers the ``GET /api/eval/buffer-trace/{session_id}`` endpoint, which recomputes
the trace cheaply from live session state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import RenderPriority
from app.eval.metrics import BufferSample
from app.memory.budget_service import Reservation
from app.queue.redis_queue import PREEMPTIBLE_LANES, EnqueueResult, EnqueueStatus
from app.scheduler.model import SchedulerSession
from app.scheduler.service import SchedulerService, ShotSource
from app.scheduler.zones import DEFAULT_VELOCITY_WPS, clamp_velocity

logger = get_logger("app.eval.buffer_trace")

#: Default simulated read: 180s of wall-clock sampled every 2.5s (a clean sawtooth).
DEFAULT_DURATION_S = 180.0
DEFAULT_TICK_S = 2.5


# --------------------------------------------------------------------------- #
# Zero-video simulation collaborators (real Protocols, no real spend)
# --------------------------------------------------------------------------- #


class DryRunBudget:
    """A budget gate for the trace SIMULATION: allows promotion, reserves no video.

    ``can_render_live`` is ``True`` and ``remaining`` is effectively unbounded so
    the watermark hysteresis (not the budget) shapes the sawtooth; but ``reserve``
    hands back a reservation for **0.0 video-seconds** (a dry run — nothing
    renders), so the trace provably draws down no video budget. The would-be
    committed video is tracked in :attr:`simulated_earmarks_s` for reporting only.
    """

    def __init__(self, *, remaining: float = 1.0e9) -> None:
        self._remaining = remaining
        self.reserve_calls = 0
        #: Actual video-seconds reserved against the budget — stays 0.0 (dry run).
        self.video_reserved = 0.0
        #: The committed video that *would* be spent in production (sawtooth area).
        self.simulated_earmarks_s = 0.0

    def can_render_live(self) -> bool:
        return True

    async def is_low(self) -> bool:
        return False

    async def remaining(self) -> float:
        return self._remaining

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        self.reserve_calls += 1
        self.simulated_earmarks_s += video_seconds
        # Reserve ZERO real video-seconds: this is a dry run; no clip is rendered.
        return Reservation(
            id=f"dryrun_{self.reserve_calls}",
            video_seconds=0.0,
            session_id=session_id,
            scene_id=scene_id,
            book_id=book_id,
        )

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        return None


class RecordingQueue:
    """Records enqueues (idempotent by ``shot_hash``) and renders nothing."""

    def __init__(self) -> None:
        self.enqueues: list[dict[str, Any]] = []
        self._known: dict[str, str] = {}

    async def enqueue(
        self,
        *,
        shot_hash: str,
        priority: RenderPriority,
        book_id: str,
        job_id: str,
        session_id: str | None = None,
        shot_id: str | None = None,
        beat_id: str | None = None,
        scene_id: str | None = None,
        cancel_token: str | None = None,
        reservation_id: str | None = None,
        reserved_video_s: float = 0.0,
        target_duration_s: float = 5.0,
        target_word: int = 0,
        prompt: str | None = None,
        now_ms: int | None = None,
    ) -> EnqueueResult:
        if shot_hash in self._known:
            return EnqueueResult(status=EnqueueStatus.EXISTING, job_id=self._known[shot_hash])
        self._known[shot_hash] = job_id
        self.enqueues.append(
            {
                "shot_hash": shot_hash,
                "priority": priority,
                "shot_id": shot_id,
                "target_word": target_word,
            }
        )
        return EnqueueResult(status=EnqueueStatus.ENQUEUED, job_id=job_id)

    async def cancel_by_token(
        self, token: str, *, lanes: Sequence[RenderPriority] | None = None
    ) -> int:
        return 0

    async def cancel_distant(
        self,
        token: str,
        *,
        focus_word: int,
        velocity_wps: float,
        threshold_s: float = 120.0,
        lanes: Sequence[RenderPriority] = PREEMPTIBLE_LANES,
    ) -> int:
        return 0

    @property
    def committed_enqueues(self) -> list[dict[str, Any]]:
        return [e for e in self.enqueues if e["priority"] is RenderPriority.COMMITTED]


class RecordingKeyframes:
    """Records keyframe ensures (the cheap, zero-video lane, §4.4)."""

    def __init__(self) -> None:
        self.ensured: list[dict[str, Any]] = []

    async def ensure(
        self,
        session: SchedulerSession,
        *,
        book_id: str,
        beat_id: str,
        target_word: int,
        prompt: str | None = None,
    ) -> None:
        self.ensured.append({"book_id": book_id, "beat_id": beat_id, "target_word": target_word})


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BufferTraceResult:
    """The simulated §4.10 sawtooth + the proof it spent no video."""

    samples: list[BufferSample] = field(default_factory=list)
    low: float = 0.0
    high: float = 0.0
    commit_horizon: float = 0.0
    velocity_wps: float = DEFAULT_VELOCITY_WPS
    #: Actual video-seconds rendered — ALWAYS 0.0 (the simulation renders nothing).
    video_seconds_spent: float = 0.0
    #: Actual video-seconds reserved against the budget — ALWAYS 0.0 (dry run).
    video_reservations_s: float = 0.0
    #: Committed video that *would* be spent in production (sawtooth area, info).
    simulated_earmarks_s: float = 0.0
    committed_promotions: int = 0
    keyframes_ensured: int = 0

    def to_contract(self) -> list[dict[str, float]]:
        """The exact ``GET /api/eval/buffer-trace/{session_id}`` array shape."""
        return [sample.to_contract() for sample in self.samples]


# --------------------------------------------------------------------------- #
# The simulation
# --------------------------------------------------------------------------- #


async def simulate_buffer_trace(
    *,
    shots: ShotSource,
    book_id: str,
    focus_word: int = 0,
    velocity_wps: float = DEFAULT_VELOCITY_WPS,
    settings: Settings | None = None,
    duration_s: float = DEFAULT_DURATION_S,
    tick_s: float = DEFAULT_TICK_S,
    keyframe_cap: int = 12,
    session_id: str | None = None,
) -> BufferTraceResult:
    """Run the real scheduler over a simulated read → the §4.10 sawtooth (zero video).

    Args:
        shots: the §4.2 source-span index seam (the real ``SourceSpanRepo`` in
            the API, or an in-memory index in tests).
        book_id: the book whose shots are being read.
        focus_word: the starting focus word ``w``.
        velocity_wps: the simulated reading velocity (clamped per §4.3).
        duration_s: total simulated wall-clock to trace.
        tick_s: spacing between samples (the §4.7 settle cadence).

    Returns:
        A :class:`BufferTraceResult` whose ``samples`` are the monotonic-time
        sawtooth and whose ``video_seconds_spent``/``video_reservations_s`` are
        both ``0.0`` by construction.
    """
    settings = settings or get_settings()
    budget = DryRunBudget()
    queue = RecordingQueue()
    keyframes = RecordingKeyframes()
    service = SchedulerService(
        queue=queue,
        budget=budget,
        shots=shots,
        keyframes=keyframes,
        store=None,
        settings=settings,
        keyframe_cap=keyframe_cap,
    )
    low, high, commit_horizon, _spec = service.watermarks

    clamped = clamp_velocity(velocity_wps)
    session = SchedulerSession(
        session_id=session_id or f"eval_trace_{book_id}",
        book_id=book_id,
        focus_word=focus_word,
        velocity_wps=clamped,
        raw_velocity_wps=abs(velocity_wps),
    )

    samples: list[BufferSample] = []
    n_ticks = max(1, int(round(duration_s / tick_s)) + 1)
    for i in range(n_ticks):
        t = round(i * tick_s, 6)
        # The reader advances at v wps; focus word = start + v*t (reading-time).
        session.focus_word = focus_word + int(round(velocity_wps * t))
        # now_ms=None => the idle-pause never fires (a continuous, attentive read).
        await service.on_event(session, allow_promotion=True, now_ms=None)
        samples.append(
            BufferSample(
                t=t,
                committed_seconds_ahead=session.committed_seconds_ahead,
                low=low,
                high=high,
            )
        )

    logger.info(
        "eval.buffer_trace",
        book_id=book_id,
        velocity_wps=clamped,
        ticks=len(samples),
        peak=max((s.committed_seconds_ahead for s in samples), default=0.0),
        video_seconds_spent=0.0,
        video_reservations_s=budget.video_reserved,
    )
    return BufferTraceResult(
        samples=samples,
        low=low,
        high=high,
        commit_horizon=commit_horizon,
        velocity_wps=clamped,
        video_seconds_spent=0.0,
        video_reservations_s=budget.video_reserved,
        simulated_earmarks_s=budget.simulated_earmarks_s,
        committed_promotions=len(queue.committed_enqueues),
        keyframes_ensured=len(keyframes.ensured),
    )


__all__ = [
    "DEFAULT_DURATION_S",
    "DEFAULT_TICK_S",
    "BufferTraceResult",
    "DryRunBudget",
    "RecordingKeyframes",
    "RecordingQueue",
    "simulate_buffer_trace",
]
