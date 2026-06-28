"""Reading-trace replay harness tests (kinora.md §4.3–§4.10, §13) — zero video.

Drives the real :class:`SchedulerService` + :class:`ReadingModel` over scripted
reader archetypes and asserts the §4.10 behaviours per archetype: a steady reader
makes a clean sawtooth with no stalls; a skimmer never promotes full video; a
thinker idle-pauses and freezes the buffer; a seeker re-seeds without stalling.
Every replay proves ``video_seconds_spent == video_reservations_s == 0.0``.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.scheduler.simulation import (
    ActionKind,
    ReaderAction,
    ReaderProfile,
    ReadingTrace,
    replay_trace,
)
from tests.test_scheduler_support import BOOK_ID, FakeShots, build_shots

_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240


def _shots(n: int = 400) -> FakeShots:
    return FakeShots(build_shots(n, spacing=10, duration_s=5.0))


# --- steady reader: the §4.10 sawtooth ------------------------------------- #


async def test_steady_reader_is_a_clean_zero_video_sawtooth() -> None:
    trace = ReaderProfile.steady(velocity_wps=4.0, duration_s=180.0)
    result = await replay_trace(
        trace, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS
    )

    occupancy = [s.committed_seconds_ahead for s in result.samples]
    assert max(occupancy) == _SETTINGS.watermark_high_s  # fills to H
    deltas = [b - a for a, b in zip(occupancy, occupancy[1:], strict=False)]
    assert any(d > 10.0 for d in deltas)  # a burst refill
    assert any(d < 0.0 for d in deltas)  # draining between bursts

    health = result.health()
    assert health.stalls == 0
    assert health.fraction_above_low >= 0.8

    # Zero-spend proof + a real sawtooth.
    assert result.video_seconds_spent == 0.0
    assert result.video_reservations_s == 0.0
    assert result.committed_promotions > 0
    assert result.simulated_earmarks_s > 0.0


async def test_replay_trains_the_prediction_model() -> None:
    trace = ReaderProfile.steady(velocity_wps=6.0, duration_s=120.0)
    result = await replay_trace(
        trace, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS
    )
    pred = result.model.predict_velocity()
    assert abs(pred.raw_mean_wps - 6.0) < 0.5  # learned the reader's true rate
    assert result.model.is_steady() is True


# --- skimmer: §4.6 suspends promotion -------------------------------------- #


async def test_skimmer_promotes_no_full_video() -> None:
    trace = ReaderProfile.skimmer(velocity_wps=16.0, duration_s=60.0)
    result = await replay_trace(
        trace, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS
    )
    # Above the clamp ceiling → unstable trajectory → keyframe ladder only.
    assert result.committed_promotions == 0
    assert result.keyframes_ensured > 0  # cheap stills still cover the path
    assert result.video_seconds_spent == 0.0


# --- thinker: §4.7 idle-pause ---------------------------------------------- #


async def test_thinker_idle_pauses_and_freezes_buffer() -> None:
    trace = ReaderProfile.thinker(
        velocity_wps=3.0, read_s=30.0, pause_s=20.0, cycles=3
    )
    result = await replay_trace(
        trace, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS
    )
    assert result.idle_ticks > 0  # the long pauses actually idle-paused
    assert result.video_seconds_spent == 0.0
    # The buffer is preserved across pauses (never crashes to a stall mid-think).
    assert result.health().stalls == 0


# --- seeker: §4.8 re-seed -------------------------------------------------- #


async def test_seeker_reseeds_without_spending_video() -> None:
    trace = ReaderProfile.seeker(
        velocity_wps=4.0, read_s=30.0, jumps=(2000, 100, 3500)
    )
    result = await replay_trace(
        trace, shots=_shots(n=500), book_id=BOOK_ID, settings=_SETTINGS
    )
    assert result.seeks == 3
    assert result.video_seconds_spent == 0.0
    assert result.video_reservations_s == 0.0
    # A backward seek (to word 100) lands on already-cheap keyframes — no stall panic.
    assert result.health().stalls <= 1


# --- variable reader is deterministic from its seed ------------------------ #


async def test_variable_reader_is_deterministic() -> None:
    trace_a = ReaderProfile.variable(base_wps=4.0, jitter=0.5, segments=8, seed=42)
    trace_b = ReaderProfile.variable(base_wps=4.0, jitter=0.5, segments=8, seed=42)
    ra = await replay_trace(trace_a, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS)
    rb = await replay_trace(trace_b, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS)
    assert [s.committed_seconds_ahead for s in ra.samples] == [
        s.committed_seconds_ahead for s in rb.samples
    ]


async def test_custom_trace_and_contract_shape() -> None:
    trace = ReadingTrace(
        actions=[
            ReaderAction(ActionKind.READ, duration_s=20.0, velocity_wps=4.0),
            ReaderAction(ActionKind.PAUSE, duration_s=5.0),
            ReaderAction(ActionKind.READ, duration_s=20.0, velocity_wps=5.0),
        ],
        label="custom",
    )
    result = await replay_trace(
        trace, shots=_shots(), book_id=BOOK_ID, settings=_SETTINGS
    )
    contract = result.to_contract()
    assert contract
    for item in contract:
        assert set(item.keys()) == {"t", "committed_seconds_ahead", "low", "high"}
