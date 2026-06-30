"""Phase profiler — clock-driven span timing + mergeability."""

from __future__ import annotations

import pytest

from app.loadtest.clock import VirtualClock
from app.loadtest.profiler import PhaseProfiler
from tests.loadtest.conftest import drive


def test_span_times_phase_on_clock() -> None:
    clock = VirtualClock()
    prof = PhaseProfiler(clock)

    async def go() -> None:
        async with prof.span("auth"):
            await clock.sleep(0.2)
        async with prof.span("canon_query"):
            await clock.sleep(0.5)

    drive(clock, go)
    assert prof.stats_for("auth").summary().mean == pytest.approx(0.2, rel=0.02)
    assert prof.stats_for("canon_query").summary().mean == pytest.approx(0.5, rel=0.02)


def test_span_records_even_on_exception() -> None:
    clock = VirtualClock()
    prof = PhaseProfiler(clock)

    async def go() -> None:
        with pytest.raises(RuntimeError):
            async with prof.span("flaky"):
                await clock.sleep(0.1)
                raise RuntimeError("boom")

    drive(clock, go)
    assert prof.stats_for("flaky").calls == 1
    assert prof.stats_for("flaky").summary().mean == pytest.approx(0.1, rel=0.02)


def test_record_direct_and_counts() -> None:
    clock = VirtualClock()
    prof = PhaseProfiler(clock)
    for _ in range(5):
        prof.record("render_enqueue", 0.03)
    st = prof.stats_for("render_enqueue")
    assert st.calls == 5
    assert st.summary().mean == pytest.approx(0.03, rel=0.02)


def test_merge_combines_profilers() -> None:
    clock = VirtualClock()
    a = PhaseProfiler(clock)
    b = PhaseProfiler(clock)
    for _ in range(3):
        a.record("p", 0.1)
    for _ in range(2):
        b.record("p", 0.1)
    a.merge_in(b)
    assert a.stats_for("p").calls == 5


def test_as_dict_is_json_shaped() -> None:
    clock = VirtualClock()
    prof = PhaseProfiler(clock)
    prof.record("auth", 0.05)
    prof.record("auth", 0.05)
    d = prof.as_dict()
    assert "auth" in d
    auth = d["auth"]
    assert auth["calls"] == 2  # type: ignore[index]
    assert auth["mean_ms"] == pytest.approx(50.0, rel=0.02)  # type: ignore[index]


def test_total_time_by_phase() -> None:
    clock = VirtualClock()
    prof = PhaseProfiler(clock)
    for _ in range(4):
        prof.record("a", 0.25)
    totals = prof.total_time_by_phase()
    assert totals["a"] == pytest.approx(1.0, rel=0.02)  # 4 * 0.25
