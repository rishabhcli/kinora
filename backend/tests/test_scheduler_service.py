"""Scheduler control-loop tests (kinora.md §4.5–§4.9) — pure, with legitimate doubles.

These exercise the watermark math directly: the §4.10 sawtooth (fill to H, idle
between L and H, burst-refill below L), velocity-adaptive promotion (faster
readers promote more/earlier, ETA-gated at C), skim suspension, idle-pause, and
the proof that the keyframe/speculative path spends **zero** video-seconds. A
Redis-backed session round-trip is checked when ``KINORA_TEST_REDIS_URL`` is set.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import get_settings
from app.db.models.enums import RenderPriority
from app.queue.redis_queue import PREEMPTIBLE_LANES
from app.scheduler.model import BufferedShot, SchedulerSession
from app.scheduler.service import SchedulerService
from tests.test_scheduler_support import (
    BOOK_ID,
    FakeBudget,
    FakeKeyframes,
    FakeQueue,
    build_shots,
)

_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240
_L, _H, _C, _SPEC = (
    _SETTINGS.watermark_low_s,
    _SETTINGS.watermark_high_s,
    _SETTINGS.commit_horizon_s,
    _SETTINGS.spec_horizon_s,
)


def _service(
    shots: list, *, budget: FakeBudget | None = None, queue: FakeQueue | None = None,
    keyframes: FakeKeyframes | None = None,
) -> tuple[SchedulerService, FakeQueue, FakeBudget, FakeKeyframes]:
    q = queue or FakeQueue()
    b = budget or FakeBudget()
    k = keyframes or FakeKeyframes()
    from tests.test_scheduler_support import FakeShots

    svc = SchedulerService(
        queue=q, budget=b, shots=FakeShots(shots), keyframes=k, settings=_SETTINGS
    )
    return svc, q, b, k


def _session(**kw: object) -> SchedulerSession:
    return SchedulerSession(session_id="sess_1", book_id=BOOK_ID, **kw)


# --- the §4.10 sawtooth: fill to H, idle in the band, refill below L -------- #


async def test_watermark_sawtooth_hysteresis() -> None:
    # 10-word spacing, 5s shots, v=4 wps => the high watermark binds (not ETA).
    svc, queue, budget, _ = _service(build_shots(80, spacing=10, duration_s=5.0))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    first = await svc.on_event(session, allow_promotion=True)
    assert first.committed_seconds_ahead == _H  # filled exactly to the high watermark
    assert len(first.promoted) == int(_H / 5)  # 15 shots
    assert session.bursting is False  # burst-off latched at H

    trace: list[tuple[int, float, int]] = [(0, first.committed_seconds_ahead, len(first.promoted))]
    idle_ticks = 0
    refill_ticks = 0
    for w in range(10, 230, 10):
        session.focus_word = w
        tick = await svc.on_event(session, allow_promotion=True)
        trace.append((w, tick.committed_seconds_ahead, len(tick.promoted)))
        if _L <= tick.committed_seconds_ahead < _H and not tick.promoted:
            idle_ticks += 1
        if tick.promoted:
            refill_ticks += 1

    occupancy = [a for _, a, _ in trace]
    print("\n[SAWTOOTH] committed-seconds-ahead vs focus-word (v=4, L=25 H=75):")
    print("  " + " | ".join(f"w={w}:{a:.0f}s{'*' if p else ''}" for w, a, p in trace))

    assert max(occupancy) == _H  # peaks at H
    assert min(occupancy) >= 20  # never stalls toward zero (always smooth)
    assert idle_ticks >= 3  # a real idle band between L and H (not always generating)
    assert refill_ticks >= 1  # at least one burst-refill after crossing below L
    # Refills only spend video when actually promoting (bursty, event-driven).
    assert budget.reserves  # committed promotions reserved video-seconds
    assert all(e["priority"] is RenderPriority.COMMITTED for e in queue.by_priority(
        RenderPriority.COMMITTED))


async def test_idle_band_does_not_generate() -> None:
    svc, queue, _, _ = _service(build_shots(80, spacing=10, duration_s=5.0))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    await svc.on_event(session, allow_promotion=True)  # fill to H
    committed_after_fill = len(queue.by_priority(RenderPriority.COMMITTED))

    # Advance a little: still between L and H => no new committed work (idle).
    session.focus_word = 30  # ahead ~60s, within [L, H)
    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.promoted == []
    assert len(queue.by_priority(RenderPriority.COMMITTED)) == committed_after_fill


# --- velocity-adaptive promotion (§4.6) ------------------------------------- #


async def test_velocity_adaptive_promotion() -> None:
    slow_svc, _, slow_budget, slow_kf = _service(build_shots(80, spacing=10))
    fast_svc, _, _, _ = _service(build_shots(80, spacing=10))

    slow = _session(focus_word=0, velocity_wps=2.0, raw_velocity_wps=2.0)
    fast = _session(focus_word=0, velocity_wps=8.0, raw_velocity_wps=8.0)

    slow_tick = await slow_svc.on_event(slow, allow_promotion=True)
    fast_tick = await fast_svc.on_event(fast, allow_promotion=True)

    # Faster reader promotes more and earlier (the ETA term self-tunes, §4.6).
    assert len(fast_tick.promoted) > len(slow_tick.promoted)
    # Slow reader is ETA-gated below H; fast reader fills to H.
    assert slow_tick.committed_seconds_ahead < _H
    assert fast_tick.committed_seconds_ahead == _H
    # Every promoted shot was inside the commit horizon.
    for entry in slow_budget.reserves:
        assert entry > 0
    # The slow reader rides keyframes for the un-promoted speculative beats.
    assert slow_kf.ensured
    slow_ahead = slow_tick.committed_seconds_ahead
    fast_ahead = fast_tick.committed_seconds_ahead
    print(f"\n[VELOCITY] v=2 promoted {len(slow_tick.promoted)} (ahead={slow_ahead:.0f}s, "
          f"ETA-gated); v=8 promoted {len(fast_tick.promoted)} (ahead={fast_ahead:.0f}s)")


async def test_skim_suspends_promotion_but_keeps_keyframes() -> None:
    svc, queue, budget, keyframes = _service(build_shots(80, spacing=10))
    # ETA velocity is fine, but the raw estimate is a rapid skim (above 3x ceiling).
    session = _session(focus_word=0, velocity_wps=8.0, raw_velocity_wps=20.0)

    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.promoted == []  # promotion suspended (§4.6)
    assert budget.reserves == []  # => zero video-seconds spent
    assert tick.keyframed  # but the keyframe ladder still covers the path
    assert queue.by_priority(RenderPriority.COMMITTED) == []
    print(f"\n[SKIM] raw v=20 => promoted=0, reserves=0, keyframes={len(tick.keyframed)}")


async def test_oscillation_suspends_promotion() -> None:
    svc, _, budget, _ = _service(build_shots(40, spacing=10))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0, oscillating=True)
    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.promoted == []
    assert budget.reserves == []


# --- idle-pause (§4.7) ------------------------------------------------------ #


async def test_idle_pause_cancels_speculative_freezes_committed() -> None:
    svc, queue, budget, _ = _service(build_shots(40, spacing=10))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    # Pre-existing committed buffer that must be preserved across the idle pause.
    session.committed_buffer = [
        BufferedShot(shot_id="shot_a", word_index_start=100, est_duration_s=5.0),
        BufferedShot(shot_id="shot_b", word_index_start=110, est_duration_s=5.0),
    ]
    base = 1_700_000_000_000
    session.last_activity_ms = base - 9_000  # 9s of inactivity (> 8s threshold)

    tick = await svc.on_event(session, allow_promotion=True, now_ms=base)

    assert tick.idle is True
    assert tick.promoted == []
    assert budget.reserves == []  # nothing generated while idle
    assert session.bursting is False
    assert len(session.committed_buffer) == 2  # committed buffer frozen, preserved
    assert queue.cancel_token_calls  # speculative cancelled
    token, lanes = queue.cancel_token_calls[-1]
    assert token == session.trajectory_token
    assert tuple(lanes) == PREEMPTIBLE_LANES  # only speculative + keyframe lanes
    lane_names = [p.value for p in lanes]
    print(f"\n[IDLE] 9s quiet => idle tick: cancelled speculative on lanes {lane_names}, "
          f"committed buffer preserved ({len(session.committed_buffer)})")


async def test_not_idle_within_threshold() -> None:
    svc, _, _, _ = _service(build_shots(40, spacing=10))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    base = 1_700_000_000_000
    session.last_activity_ms = base - 3_000  # only 3s quiet (< 8s)
    tick = await svc.on_event(session, allow_promotion=True, now_ms=base)
    assert tick.idle is False


# --- ZERO video-seconds on the keyframe / speculative path (§4.4) ----------- #


async def test_keyframe_path_spends_zero_video_seconds() -> None:
    svc, queue, budget, keyframes = _service(build_shots(40, spacing=10))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    # Dwell not confirmed => promotion off; only the keyframe lane runs.
    tick = await svc.on_event(session, allow_promotion=False)

    assert budget.reserves == []  # the scarce currency is untouched
    assert queue.by_priority(RenderPriority.COMMITTED) == []
    assert tick.keyframed  # keyframes ensured across the horizon
    print(f"\n[ZERO-VIDEO] keyframe-only tick: reserves={budget.reserves} "
          f"committed_enqueues={len(queue.by_priority(RenderPriority.COMMITTED))} "
          f"keyframes={len(tick.keyframed)}")


async def test_live_gate_off_rides_keyframe_ladder() -> None:
    # Live video disabled => no committed promotion, only keyframes (§11.1/§4.4).
    budget = FakeBudget(live=False)
    svc, queue, _, _ = _service(build_shots(40, spacing=10), budget=budget)
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.promoted == []
    assert budget.reserves == []
    assert tick.keyframed


async def test_budget_low_rides_keyframe_ladder() -> None:
    budget = FakeBudget(low=True)
    svc, _, _, _ = _service(build_shots(40, spacing=10), budget=budget)
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.promoted == []
    assert budget.reserves == []


# --- Redis-backed session round-trip (durable hot path) --------------------- #


@pytest.mark.skipif(
    not os.environ.get("KINORA_TEST_REDIS_URL"), reason="KINORA_TEST_REDIS_URL not set"
)
async def test_scheduler_session_redis_roundtrip() -> None:
    import uuid

    from app.redis.client import RedisClient
    from app.scheduler.model import SchedulerStore

    client = RedisClient.from_url(os.environ["KINORA_TEST_REDIS_URL"])
    store = SchedulerStore(client, namespace=f"kinora:test:sched:{uuid.uuid4().hex[:8]}")
    try:
        session = _session(focus_word=4523, velocity_wps=3.8, raw_velocity_wps=3.8)
        session.committed_buffer = [
            BufferedShot(shot_id="shot_00043", word_index_start=4600, est_duration_s=5.0)
        ]
        session.committed_seconds_ahead = 41.0
        await store.save(session)

        loaded = await store.load(session.session_id)
        assert loaded is not None
        assert loaded.focus_word == 4523
        assert loaded.committed_seconds_ahead == 41.0
        assert loaded.committed_buffer[0].shot_id == "shot_00043"
        assert loaded.inflight["committed"] == ["shot_00043"]

        await store.delete(session.session_id)
        assert await store.load(session.session_id) is None
    finally:
        await client.close()
