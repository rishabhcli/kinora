"""The live ``buffer_state`` surfacing (kinora.md §5.3/§5.6).

Every Scheduler control tick publishes a small ``buffer_state`` event so the
client's buffer hairline can fill toward ``H`` and the zone badge can name the
representation the reader is seeing. These tests pin the contract: the watermark
fields, the burst/idle flags, and the viewer zone derived from the nearest
upcoming shot's ETA — including the skim and low-budget downgrades to a preview
still (which hold even with the live-video gate off, since the zone mirrors the
§4.6 promotion *decision*, not the byte-level render state).
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.scheduler.intent import IntentController
from app.scheduler.model import SchedulerSession, SchedulerStore
from app.scheduler.service import SchedulerService
from tests.test_scheduler_support import (
    BOOK_ID,
    FakeBudget,
    FakeKeyframes,
    FakeQueue,
    FakeRedis,
    FakeShots,
    build_shots,
)

_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240
_L, _H, _C, _SPEC = (
    _SETTINGS.watermark_low_s,
    _SETTINGS.watermark_high_s,
    _SETTINGS.commit_horizon_s,
    _SETTINGS.spec_horizon_s,
)


class RecordingEvents:
    """A §5.6 publisher double that records every published event."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, session_id: str, message: dict[str, Any]) -> int:
        self.published.append((session_id, message))
        return 1

    def last(self, event: str) -> dict[str, Any] | None:
        for _sid, msg in reversed(self.published):
            if msg.get("event") == event:
                return msg
        return None


def _service(
    shots: list, *, budget: FakeBudget | None = None, events: RecordingEvents | None = None
) -> tuple[SchedulerService, RecordingEvents]:
    ev = events or RecordingEvents()
    svc = SchedulerService(
        queue=FakeQueue(),
        budget=budget or FakeBudget(),
        shots=FakeShots(shots),
        keyframes=FakeKeyframes(),
        settings=_SETTINGS,
        events=ev,
    )
    return svc, ev


def _session(**kw: object) -> SchedulerSession:
    return SchedulerSession(session_id="sess_buf", book_id=BOOK_ID, **kw)


async def test_active_tick_publishes_buffer_state_with_watermarks() -> None:
    svc, ev = _service(build_shots(80, spacing=10, duration_s=5.0))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    tick = await svc.on_event(session, allow_promotion=True)

    msg = ev.last("buffer_state")
    assert msg is not None
    # The watermarks the hairline is measured against (§4.5/§4.6).
    assert msg["low"] == _L and msg["high"] == _H and msg["commit_horizon"] == _C
    # The occupancy the hairline fills toward H (mirrors the tick).
    assert msg["committed_seconds_ahead"] == round(tick.committed_seconds_ahead, 3)
    assert msg["idle"] is False
    assert msg["bursting"] == session.bursting
    # A stable forward reader, near shot, live budget → full film.
    assert msg["zone"] == "committed"
    # Enriched surfacing (§5.3): authoritative velocity + ETA + inflight + burst.
    assert msg["velocity_wps"] == round(session.velocity_wps, 3)
    assert msg["eta_next_s"] is not None and msg["eta_next_s"] >= 0
    assert msg["promoted"] == len(tick.promoted) and msg["promoted"] > 0
    assert msg["inflight_committed"] == len(session.inflight["committed"])
    assert msg["inflight_speculative"] == len(session.inflight["speculative"])


async def test_skim_publishes_preview_still_zone() -> None:
    # ETA velocity is fine, but the raw estimate is a rapid skim (above the 3x ceiling).
    svc, ev = _service(build_shots(80, spacing=10))
    session = _session(focus_word=0, velocity_wps=12.0, raw_velocity_wps=20.0)

    await svc.on_event(session, allow_promotion=False)

    msg = ev.last("buffer_state")
    assert msg is not None
    # Skim suspends promotion → the reader rides the keyframe ladder (preview still).
    assert msg["zone"] == "speculative"


async def test_low_budget_downgrades_zone() -> None:
    svc, ev = _service(build_shots(80, spacing=10), budget=FakeBudget(low=True))
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    await svc.on_event(session, allow_promotion=True)

    msg = ev.last("buffer_state")
    assert msg is not None
    # Budget-aware degradation: even a near, stable shot rides the ladder (§11.1).
    assert msg["zone"] == "speculative"


async def test_far_gap_publishes_planning_ahead_zone() -> None:
    # The only shot is far beyond the speculative horizon at this velocity.
    far = build_shots(1, spacing=4000)  # one shot at word 4000
    svc, ev = _service(far)
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)

    await svc.on_event(session, allow_promotion=True)

    msg = ev.last("buffer_state")
    assert msg is not None
    assert msg["zone"] == "cold"  # nothing near → planning ahead


async def test_idle_tick_publishes_idle_state() -> None:
    svc, ev = _service(build_shots(80, spacing=10))
    session = _session(
        focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0, last_activity_ms=0
    )

    # now far past the idle-pause threshold (§4.7) → the idle branch runs.
    await svc.on_event(session, allow_promotion=False, now_ms=10_000)

    msg = ev.last("buffer_state")
    assert msg is not None
    assert msg["idle"] is True
    assert msg["bursting"] is False


async def test_no_events_publisher_is_a_noop() -> None:
    # The tests' default service has no publisher — on_event must not crash.
    svc = SchedulerService(
        queue=FakeQueue(),
        budget=FakeBudget(),
        shots=FakeShots(build_shots(10, spacing=10)),
        keyframes=FakeKeyframes(),
        settings=_SETTINGS,
    )
    session = _session(focus_word=0, velocity_wps=4.0, raw_velocity_wps=4.0)
    tick = await svc.on_event(session, allow_promotion=True)
    assert tick.idle is False


async def test_idle_sweep_quiets_the_hairline() -> None:
    """The periodic idle-sweeper (§4.7) surfaces ``idle=true`` with no scroll.

    The api process runs :meth:`IntentController.sweep_idle` on a timer; once the
    reader has been quiet ≥ 8s it must publish a ``buffer_state`` with ``idle``
    true so the hairline goes quiet on its own (the reader never touched a thing).
    """
    ev = RecordingEvents()
    store = SchedulerStore(FakeRedis())
    svc = SchedulerService(
        queue=FakeQueue(),
        budget=FakeBudget(),
        shots=FakeShots(build_shots(40, spacing=10)),
        keyframes=FakeKeyframes(),
        store=store,
        settings=_SETTINGS,
        events=ev,
    )
    controller = IntentController(service=svc, store=store, settings=_SETTINGS)

    session = await controller.ensure_session("sess_idle", BOOK_ID, focus_word=0)
    session.last_activity_ms = 1_000
    await store.save(session)

    # 9s of silence after the last activity → past the 8s idle-pause threshold.
    tick = await controller.sweep_idle("sess_idle", now_ms=10_000)
    assert tick is not None and tick.idle is True

    msg = ev.last("buffer_state")
    assert msg is not None
    assert msg["idle"] is True
    assert msg["bursting"] is False
