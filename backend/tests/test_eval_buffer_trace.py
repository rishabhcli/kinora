"""The §4.5/§4.10 buffer-trace: a real-scheduler sawtooth that spends zero video.

Drives the real :class:`SchedulerService` (via :func:`simulate_buffer_trace`)
over an in-memory source-span index and asserts the §4.10 behaviour: monotonic
time, fill to ``H``, burst-refill below ``L``, never stalling toward zero — and,
critically, **zero video-seconds reserved or spent**.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.eval.buffer_trace import simulate_buffer_trace
from app.eval.metrics import buffer_health
from tests.test_scheduler_support import BOOK_ID, FakeShots, build_shots

_SETTINGS = get_settings()  # L=25, H=75, C=45, SPEC=240


async def test_buffer_trace_is_a_zero_video_sawtooth() -> None:
    shots = FakeShots(build_shots(120, spacing=10, duration_s=5.0))
    result = await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        focus_word=0,
        velocity_wps=4.0,
        settings=_SETTINGS,
        duration_s=120.0,
        tick_s=2.5,
    )

    occupancy = [s.committed_seconds_ahead for s in result.samples]
    times = [s.t for s in result.samples]
    print("\n[BUFFER-TRACE] committed-seconds-ahead over wall-clock (v=4, L=25 H=75):")
    print("  " + " | ".join(f"{t:.0f}s:{a:.0f}" for t, a in zip(times, occupancy, strict=True)))

    # Monotonic time.
    assert times == sorted(times)
    assert all(b > a for a, b in zip(times, times[1:], strict=False))

    # Watermarks reported on every sample (the contract carries L and H).
    assert all(s.low == _SETTINGS.watermark_low_s for s in result.samples)
    assert all(s.high == _SETTINGS.watermark_high_s for s in result.samples)

    # Sawtooth: peaks exactly at H, never approaches a stall, and refills in bursts.
    assert max(occupancy) == _SETTINGS.watermark_high_s
    assert min(occupancy) >= _SETTINGS.watermark_low_s - 5.0  # at most one shot below L
    deltas = [b - a for a, b in zip(occupancy, occupancy[1:], strict=False)]
    assert any(d > 10.0 for d in deltas)  # a burst refill (jump up to H)
    assert any(d < 0.0 for d in deltas)  # draining between bursts

    # THE zero-video proof: no real video reserved and nothing rendered.
    assert result.video_seconds_spent == 0.0
    assert result.video_reservations_s == 0.0
    # It really did exercise committed promotion (the sawtooth is real, not faked).
    assert result.committed_promotions > 0
    assert result.simulated_earmarks_s > 0.0  # would-be committed video, never spent


async def test_buffer_trace_buffer_health_no_stalls() -> None:
    shots = FakeShots(build_shots(120, spacing=10, duration_s=5.0))
    result = await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        focus_word=0,
        velocity_wps=4.0,
        settings=_SETTINGS,
        duration_s=120.0,
        tick_s=2.5,
    )
    health = buffer_health(result.samples, low_watermark=result.low)
    assert health.stalls == 0  # §13 target: zero visible stalls
    assert health.fraction_above_low >= 0.8  # mostly above L (coarse 2.5s ticks dip briefly)


async def test_buffer_trace_fast_reader_still_zero_video() -> None:
    # A faster reader promotes more/earlier (§4.6) but still spends no real video.
    shots = FakeShots(build_shots(200, spacing=10, duration_s=5.0))
    result = await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        focus_word=0,
        velocity_wps=8.0,
        settings=_SETTINGS,
        duration_s=60.0,
        tick_s=2.5,
    )
    assert result.video_seconds_spent == 0.0
    assert result.video_reservations_s == 0.0
    assert max(s.committed_seconds_ahead for s in result.samples) == _SETTINGS.watermark_high_s


async def test_buffer_trace_to_contract_shape() -> None:
    shots = FakeShots(build_shots(40, spacing=10, duration_s=5.0))
    result = await simulate_buffer_trace(
        shots=shots,
        book_id=BOOK_ID,
        velocity_wps=4.0,
        settings=_SETTINGS,
        duration_s=30.0,
        tick_s=2.5,
    )
    contract = result.to_contract()
    assert isinstance(contract, list) and contract
    for item in contract:
        assert set(item.keys()) == {"t", "committed_seconds_ahead", "low", "high"}
        assert all(isinstance(v, float) for v in item.values())
