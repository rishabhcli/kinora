"""Intent debounce/dwell/idle/seek tests (kinora.md §4.7/§4.8).

Drives the real :class:`IntentController` over a :class:`SchedulerService` with
legitimate doubles and an in-memory Redis-backed :class:`SchedulerStore` (so the
session round-trips through real serialization each call). No external infra.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.intent import IntentController
from app.scheduler.model import BufferedShot, SchedulerSession, SchedulerStore
from app.scheduler.service import SchedulerService
from app.scheduler.zones import DEFAULT_VELOCITY_WPS
from tests.test_scheduler_support import (
    BOOK_ID,
    FakeBudget,
    FakeKeyframes,
    FakeQueue,
    FakeRedis,
    FakeShots,
    build_shots,
)


def _controller(
    shots: list | None = None,
) -> tuple[IntentController, SchedulerStore, FakeQueue, FakeBudget, FakeKeyframes]:
    shots = shots if shots is not None else build_shots(120, spacing=10)
    queue = FakeQueue()
    budget = FakeBudget()
    keyframes = FakeKeyframes()
    store = SchedulerStore(FakeRedis())
    settings = get_settings()
    service = SchedulerService(
        queue=queue, budget=budget, shots=FakeShots(shots), keyframes=keyframes,
        store=store, settings=settings,
    )
    controller = IntentController(service=service, store=store, settings=settings)
    return controller, store, queue, budget, keyframes


# --- scroll-settle debounce (§4.7) ------------------------------------------ #


async def test_scroll_settle_debounce_coalesces_rapid_intents() -> None:
    ctrl, _, queue, _, _ = _controller()
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)

    r1 = await ctrl.handle_intent_update("s", focus_word=10, velocity=4.0, now_ms=0)
    assert r1.settled is True and r1.tick is not None

    # 100ms later (< 200ms debounce): coalesced, no heavy control tick.
    r2 = await ctrl.handle_intent_update("s", focus_word=15, velocity=4.0, now_ms=100)
    assert r2.settled is False
    assert r2.tick is None
    assert r2.session.focus_word == 15  # latest position still tracked

    # 300ms after the last *settled* intent: processed again.
    r3 = await ctrl.handle_intent_update("s", focus_word=40, velocity=4.0, now_ms=300)
    assert r3.settled is True and r3.tick is not None
    print("\n[DEBOUNCE] settled@0 -> deferred@100ms (<200ms) -> settled@300ms")


# --- dwell confirmation (§4.7) ---------------------------------------------- #


async def test_dwell_requires_two_forward_windows_before_promotion() -> None:
    ctrl, _, queue, budget, keyframes = _controller()
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)

    # First settled forward window: dwell not yet confirmed -> no promotion.
    r1 = await ctrl.handle_intent_update("s", focus_word=20, velocity=4.0, now_ms=0)
    assert r1.allow_promotion is False
    assert r1.tick is not None and r1.tick.promoted == []
    assert r1.tick.keyframed  # keyframe ladder still runs
    assert budget.reserves == []  # nothing committed yet

    # Second consecutive forward window: dwell confirmed -> promote.
    r2 = await ctrl.handle_intent_update("s", focus_word=40, velocity=4.0, now_ms=300)
    assert r2.allow_promotion is True
    assert r2.tick is not None and r2.tick.promoted
    assert budget.reserves  # committed promotions now reserve video-seconds
    print(f"\n[DWELL] window#1 -> no promote (cf=1); window#2 -> promoted "
          f"{len(r2.tick.promoted)} (cf=2)")


async def test_overshoot_resets_dwell() -> None:
    ctrl, _, _, budget, _ = _controller()
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)

    await ctrl.handle_intent_update("s", focus_word=20, velocity=4.0, now_ms=0)  # forward, cf=1
    # Scroll back (overshoot/return): direction flip resets dwell + flags skim.
    r2 = await ctrl.handle_intent_update("s", focus_word=10, velocity=4.0, now_ms=300)
    assert r2.session.oscillating is True
    assert r2.session.consecutive_forward == 0
    assert r2.allow_promotion is False
    assert budget.reserves == []  # a flick-and-return renders nothing


# --- idle-pause (§4.7) ------------------------------------------------------ #


async def test_idle_pause_after_eight_seconds() -> None:
    ctrl, _, queue, _, _ = _controller()
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)
    await ctrl.handle_intent_update("s", focus_word=20, velocity=4.0, now_ms=1_000)

    # No activity for 9s -> idle sweep halts speculation.
    tick = await ctrl.sweep_idle("s", now_ms=10_000)
    assert tick is not None and tick.idle is True
    assert queue.cancel_token_calls  # speculative cancelled on the trajectory token


# --- seek (§4.8) ------------------------------------------------------------ #


async def test_seek_cancels_distant_reseeds_and_bridges() -> None:
    ctrl, store, queue, _, keyframes = _controller(build_shots(120, spacing=10))
    # Seed a session with a committed buffer near word 0 and a known trajectory.
    seeded = SchedulerSession(
        session_id="s",
        book_id=BOOK_ID,
        focus_word=0,
        trajectory_token="traj_old",
        committed_buffer=[
            BufferedShot(shot_id="old_1", word_index_start=50, est_duration_s=5.0),
            BufferedShot(shot_id="old_2", word_index_start=60, est_duration_s=5.0),
        ],
        committed_seconds_ahead=10.0,
    )
    await store.save(seeded)

    res = await ctrl.handle_seek("s", word=2_000, now_ms=5_000)

    # 1. Cancel in-flight speculative now far from the new position (old token).
    assert queue.cancel_distant_calls
    call = queue.cancel_distant_calls[-1]
    assert call["focus_word"] == 2_000 and call["token"] == "traj_old"
    assert res.old_token == "traj_old"

    # 2. Re-seed: new focus, fresh trajectory token, velocity reset, buffer pruned.
    assert res.session.focus_word == 2_000
    assert res.session.trajectory_token != "traj_old"
    assert res.session.velocity_wps == DEFAULT_VELOCITY_WPS
    assert res.session.fresh_samples_needed == 2
    assert res.session.committed_buffer == []  # old near-zero buffer is now useless

    # 3. Bridge: the new position's keyframe is ensured immediately.
    assert res.bridge_beat is not None
    assert res.bridge_beat in keyframes.beats
    print(f"\n[SEEK] -> word 2000: cancel_distant(old token), buffer pruned, "
          f"velocity reset to {res.session.velocity_wps}, bridge keyframe {res.bridge_beat}")


async def test_seek_resets_velocity_until_two_fresh_samples() -> None:
    ctrl, _, _, _, _ = _controller(build_shots(400, spacing=10))
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)
    await ctrl.handle_seek("s", word=400, now_ms=1_000)

    # First two post-seek samples are ignored (default velocity), the third trusted.
    r1 = await ctrl.handle_intent_update("s", focus_word=410, velocity=10.0, now_ms=2_000)
    assert r1.session.velocity_wps == DEFAULT_VELOCITY_WPS
    r2 = await ctrl.handle_intent_update("s", focus_word=420, velocity=10.0, now_ms=3_000)
    assert r2.session.velocity_wps == DEFAULT_VELOCITY_WPS
    r3 = await ctrl.handle_intent_update("s", focus_word=430, velocity=10.0, now_ms=4_000)
    assert r3.session.velocity_wps == 10.0  # clamped measured velocity now trusted
    print("\n[SEEK velocity] default, default, then measured (10 wps) after 2 fresh samples")


async def test_seek_refills_from_new_position() -> None:
    ctrl, _, queue, budget, _ = _controller(build_shots(400, spacing=10))
    await ctrl.ensure_session("s", BOOK_ID, focus_word=0)
    res = await ctrl.handle_seek("s", word=1_000, now_ms=1_000)
    # Re-seed re-runs the watermark fill from the new position (§4.8).
    assert res.tick is not None and res.tick.promoted
    assert budget.reserves  # committed renders reserved near the new position
