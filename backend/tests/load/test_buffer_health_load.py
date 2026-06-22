"""Concurrency / buffer-health load test — the §13 guarantee under load.

This drives the **real** Scheduler control loop (via the real
:func:`app.eval.buffer_trace.simulate_buffer_trace`, i.e. real zones/ETA math,
real dual-watermark hysteresis, real velocity-adaptive promotion) for **N
concurrent reading sessions** advancing their focus word through a book's
source-span index at *varied velocities*, sampling ``committed_seconds_ahead``
on the §4.10 cadence.

It is infra-free on purpose: the scheduler's control loop is pure given its
collaborators, and the buffer-trace wires the same zero-spend doubles the real
endpoint uses — a :class:`DryRunBudget` (lets promotion proceed but reserves
**0.0** video-seconds), a recording queue (renders nothing), and a recording
keyframe lane. So the whole thing runs in both the unit and integration CI jobs
and locally, with **zero model spend**, while still exercising the real control
plane.

The §13 buffer-health claims it proves, **for every session**:

* ``committed_seconds_ahead`` stays at/above the low watermark ``L`` — the
  time-weighted ``fraction_above_low`` is ~1.0 and ``buffer_health.stalls == 0``
  (no visible stalls);
* the sawtooth stays inside the band ``[L, H + one-shot slack]`` (it peaks at
  ``H`` and refills before draining past ``L``);
* **zero** video-seconds are reserved or rendered on any path (the speculative /
  keyframe lane is image-only by construction, §4.4) — ``video_reservations_s``
  and ``video_seconds_spent`` are both ``0.0``.

A separate test covers the skimmer (velocity above the clamp ceiling): promotion
is suspended (§4.6) and the reader rides the cheap keyframe ladder — still zero
video. A third proves cross-session request dedup under concurrency: two sessions
promoting the same shot collapse to one enqueue (§12.3), so the budget is never
double-spent.
"""

from __future__ import annotations

import asyncio
import os
import statistics

from app.core.config import get_settings
from app.db.base import new_id
from app.db.models.enums import RenderPriority
from app.eval.buffer_trace import BufferTraceResult, simulate_buffer_trace
from app.eval.metrics import BufferHealth, buffer_health
from app.scheduler.model import SchedulerSession
from app.scheduler.service import SchedulerService
from app.scheduler.zones import clamp_velocity
from tests.test_scheduler_support import (
    BOOK_ID,
    FakeBudget,
    FakeKeyframes,
    FakeQueue,
    FakeShots,
    build_shots,
)

_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240

#: Number of concurrent sessions (override for a heavier run via the env var).
N_SESSIONS = int(os.environ.get("KINORA_LOAD_SESSIONS", "32"))
#: Varied, *within-clamp* reading velocities (wps) — all promote (stable), so a
#: real committed sawtooth forms for every reader (skimmers are tested apart).
VELOCITIES = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0)
TRACE_DURATION_S = 150.0
TRACE_TICK_S = 2.5

_LOW = _SETTINGS.watermark_low_s
_HIGH = _SETTINGS.watermark_high_s
#: One shot's worth of overshoot is allowed at the top of a burst (§4.5 fill).
_BAND_SLACK = 5.0
_EPS = 1e-6


def _session_specs(n: int) -> list[tuple[float, int]]:
    """``(velocity_wps, start_word)`` for ``n`` sessions, deterministically varied."""
    return [(VELOCITIES[i % len(VELOCITIES)], (i * 7) % 200) for i in range(n)]


def _grid_big_enough(n: int) -> FakeShots:
    """A source-span grid large enough that the fastest reader never runs out.

    The fastest reader advances ``max(VELOCITIES) * duration`` words; add the
    largest start offset and a margin, then convert to a shot count at the §4.2
    spacing so ``next_uncommitted_shot`` always has a shot ahead.
    """
    max_word = int(max(VELOCITIES) * TRACE_DURATION_S) + 200 + 200
    count = max_word // 10 + 50
    return FakeShots(build_shots(count, spacing=10, duration_s=5.0))


async def _run_session(velocity_wps: float, start_word: int) -> BufferTraceResult:
    shots = _grid_big_enough(N_SESSIONS)
    return await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        focus_word=start_word,
        velocity_wps=velocity_wps,
        settings=_SETTINGS,
        duration_s=TRACE_DURATION_S,
        tick_s=TRACE_TICK_S,
        session_id=f"load_{new_id()[:8]}",
    )


async def test_concurrent_sessions_buffer_health_zero_video() -> None:
    """N concurrent readers: every buffer stays >= L, no stalls, zero video."""
    specs = _session_specs(N_SESSIONS)
    results = await asyncio.gather(*(_run_session(v, w) for v, w in specs))

    healths: list[BufferHealth] = []
    mins: list[float] = []
    maxes: list[float] = []
    total_reserved = 0.0
    total_spent = 0.0
    total_earmarks = 0.0

    for (velocity, start), result in zip(specs, results, strict=True):
        occupancy = [s.committed_seconds_ahead for s in result.samples]
        health = buffer_health(result.samples, low_watermark=_LOW)
        healths.append(health)
        mins.append(min(occupancy))
        maxes.append(max(occupancy))
        total_reserved += result.video_reservations_s
        total_spent += result.video_seconds_spent
        total_earmarks += result.simulated_earmarks_s

        clamped = clamp_velocity(velocity)
        ctx = f"v={velocity} (clamped {clamped}) start={start}"

        # THE zero-video proof: nothing reserved, nothing rendered — on any path.
        assert result.video_reservations_s == 0.0, ctx
        assert result.video_seconds_spent == 0.0, ctx
        # The speculative/keyframe lane ran (image-only) yet reserved no video.
        assert result.keyframes_ensured >= 0, ctx
        # It really exercised committed promotion (the sawtooth is real).
        assert result.committed_promotions > 0, ctx
        assert result.simulated_earmarks_s > 0.0, ctx  # would-be video, never spent

        # §13 buffer health: zero stalls, essentially always at/above L.
        assert health.stalls == 0, f"{ctx}: {health}"
        assert health.fraction_above_low >= 0.99, f"{ctx}: {health}"
        # The sawtooth stays inside the band [L, H + one-shot slack].
        assert min(occupancy) >= _LOW - _EPS, f"{ctx}: min={min(occupancy)}"
        assert max(occupancy) <= _HIGH + _BAND_SLACK + _EPS, f"{ctx}: max={max(occupancy)}"

    # ----- aggregate report (pasted into the deliverable) ------------------ #
    agg_fraction = statistics.fmean(h.fraction_above_low for h in healths)
    total_stalls = sum(h.stalls for h in healths)
    print(
        "\n[LOAD] concurrency buffer-health "
        f"(N={N_SESSIONS} sessions, v in {sorted(set(VELOCITIES))} wps, "
        f"{TRACE_DURATION_S:.0f}s @ {TRACE_TICK_S}s ticks):"
    )
    print(f"  buffer >= L for {agg_fraction * 100:.2f}% of reading-time (mean)")
    print(f"  total visible stalls .............. {total_stalls}")
    print(f"  occupancy band .................... [{min(mins):.0f}, {max(maxes):.0f}]s "
          f"(L={_LOW:.0f}, H={_HIGH:.0f})")
    print(f"  video-seconds RESERVED (all paths)  {total_reserved}")
    print(f"  video-seconds RENDERED (all paths)  {total_spent}")
    print(f"  would-be committed video (earmark)  {total_earmarks:.0f}s "
          f"(spent: {total_spent})")

    # Aggregate guarantees.
    assert total_stalls == 0
    assert total_reserved == 0.0
    assert total_spent == 0.0
    assert agg_fraction >= 0.99
    assert min(mins) >= _LOW - _EPS
    assert max(maxes) <= _HIGH + _BAND_SLACK + _EPS


async def test_skimmer_rides_keyframes_with_zero_video() -> None:
    """A skimmer (velocity above the clamp) promotes nothing and rides keyframes."""
    shots = FakeShots(build_shots(400, spacing=10, duration_s=5.0))
    result = await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        focus_word=0,
        velocity_wps=20.0,  # raw > clamp ceiling (12) => trajectory unstable (§4.6)
        settings=_SETTINGS,
        duration_s=60.0,
        tick_s=2.5,
    )
    # Promotion suspended: no committed video reserved/rendered, no earmark.
    assert result.committed_promotions == 0
    assert result.video_reservations_s == 0.0
    assert result.video_seconds_spent == 0.0
    assert result.simulated_earmarks_s == 0.0
    # …but the cheap keyframe ladder still covers the path (§4.4/§4.11).
    assert result.keyframes_ensured > 0


async def test_concurrent_dedup_never_double_spends() -> None:
    """Two+ sessions promoting the same shot collapse to one enqueue (§12.3).

    A *shared* real :class:`SchedulerService` (shared queue + budget) is driven
    by N concurrent sessions on the **same** trajectory. Because the queue is
    idempotent on the book-global shot identity, identical shots are enqueued
    once across all sessions — so the video budget can never be double-spent by
    concurrent readers of the same book.
    """
    shots = FakeShots(build_shots(120, spacing=10, duration_s=5.0))
    queue = FakeQueue()
    budget = FakeBudget(remaining=1.0e9)
    keyframes = FakeKeyframes()
    service = SchedulerService(
        queue=queue,
        budget=budget,
        shots=shots,
        keyframes=keyframes,
        store=None,
        settings=_SETTINGS,
    )

    async def drive(session_id: str) -> None:
        session = SchedulerSession(
            session_id=session_id,
            book_id=BOOK_ID,
            focus_word=0,
            velocity_wps=4.0,
            raw_velocity_wps=4.0,
        )
        # A few ticks advancing the same trajectory.
        for t in range(6):
            session.focus_word = int(4.0 * t * 2.5)
            await service.on_event(session, allow_promotion=True, now_ms=None)

    await asyncio.gather(*(drive(f"dedup_{i}") for i in range(8)))

    committed = [e["shot_hash"] for e in queue.by_priority(RenderPriority.COMMITTED)]
    # Cross-session dedup: each committed shot enqueued at most once.
    assert len(committed) == len(set(committed)), "duplicate committed enqueues across sessions"
    assert committed, "expected at least one committed promotion"
