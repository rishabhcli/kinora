"""The deterministic engine: PRNG, virtual clock, discrete-event loop (kinora.md
§12 — built for flaky async work).

Proves the three guarantees the whole simulator rests on: one seeded source of
randomness (splittable, stable), one virtual clock (monotonic, free reads), and
one single-threaded event loop with strict ``(time, seq)`` ordering. If any of
these wobbled, "same seed → same run" would be a lie and every higher-level proof
would be worthless.
"""

from __future__ import annotations

import pytest

from app.verification.simulation.core import EventLoop, Prng, SimClock

# --------------------------------------------------------------------------- #
# PRNG determinism + splitting
# --------------------------------------------------------------------------- #


def test_prng_same_seed_same_stream() -> None:
    a = Prng(1234)
    b = Prng(1234)
    assert [a.random() for _ in range(64)] == [b.random() for _ in range(64)]


def test_prng_different_seed_diverges() -> None:
    a = Prng(1)
    b = Prng(2)
    assert [a.random() for _ in range(8)] != [b.random() for _ in range(8)]


def test_prng_split_is_independent_and_label_stable() -> None:
    root = Prng(42)
    net = root.split("network")
    disk = root.split("disk")
    # Two labelled children diverge from each other...
    assert [net.random() for _ in range(8)] != [disk.random() for _ in range(8)]
    # ...and the same label off a same-seeded parent reproduces exactly.
    net2 = Prng(42).split("network")
    Prng(42).split("disk")  # advancing a sibling split must not perturb 'network'
    assert net2.random() == Prng(42).split("network").random()


def test_prng_chance_matches_probability() -> None:
    prng = Prng(7)
    hits = sum(prng.chance(0.25) for _ in range(10_000))
    assert 2_200 < hits < 2_800  # ~25% within tolerance


def test_prng_randint_inclusive_bounds() -> None:
    prng = Prng(99)
    vals = {prng.randint(3, 5) for _ in range(500)}
    assert vals == {3, 4, 5}


def test_prng_hexid_is_deterministic_and_prefixed() -> None:
    a = Prng(5).hexid("shot")
    b = Prng(5).hexid("shot")
    assert a == b
    assert a.startswith("shot_")


# --------------------------------------------------------------------------- #
# SimClock
# --------------------------------------------------------------------------- #


def test_clock_starts_at_zero_and_reads_are_free() -> None:
    clock = SimClock()
    assert clock.now_ms == 0
    assert clock.now_ms == 0  # reading does not advance


def test_clock_is_monotonic() -> None:
    clock = SimClock()
    clock.advance_to(100)
    clock.advance_to(50)  # backward jump ignored
    assert clock.now_ms == 100
    clock.advance_to(150)
    assert clock.now_ms == 150


def test_clock_callables_track_the_clock() -> None:
    clock = SimClock()
    ms = clock.as_callable_ms()
    s = clock.as_callable_s()
    clock.advance_to(2_500)
    assert ms() == 2_500
    assert s() == 2.5


# --------------------------------------------------------------------------- #
# EventLoop ordering + cancellation
# --------------------------------------------------------------------------- #


def test_event_loop_fires_in_time_order_then_fifo() -> None:
    loop = EventLoop(SimClock())
    fired: list[tuple[str, int]] = []
    loop.call_at(100, lambda t: fired.append(("a", t)))
    loop.call_at(50, lambda t: fired.append(("b", t)))
    loop.call_at(100, lambda t: fired.append(("c", t)))  # ties 'a' → FIFO after it
    loop.run_until_idle()
    assert fired == [("b", 50), ("a", 100), ("c", 100)]
    assert loop.clock.now_ms == 100


def test_event_loop_cancel_skips_event() -> None:
    loop = EventLoop(SimClock())
    fired: list[str] = []
    loop.call_at(10, lambda _t: fired.append("keep"))
    handle = loop.call_at(20, lambda _t: fired.append("drop"))
    handle.cancel()
    assert handle.cancelled
    loop.run_until_idle()
    assert fired == ["keep"]


def test_event_loop_cannot_schedule_into_the_past() -> None:
    clock = SimClock()
    loop = EventLoop(clock)
    loop.call_at(100, lambda _t: None)
    loop.step()  # advances to 100
    handle = loop.call_at(50, lambda _t: None)  # clamped to now (100)
    assert handle.fire_at_ms == 100


def test_event_loop_run_until_stops_at_deadline_and_keeps_clock() -> None:
    clock = SimClock()
    loop = EventLoop(clock)
    fired: list[int] = []
    loop.call_at(50, lambda t: fired.append(t))
    loop.call_at(150, lambda t: fired.append(t))  # past the deadline
    loop.run_until(100)
    assert fired == [50]
    assert clock.now_ms == 100  # clock advanced to the deadline even though last event was at 50
    assert loop.pending == 1


def test_event_loop_livelock_guard_raises() -> None:
    loop = EventLoop(SimClock())

    def _spin(_t: int) -> None:
        loop.call_at(loop.clock.now_ms, _spin)  # reschedules at the same instant forever

    loop.call_at(0, _spin)
    with pytest.raises(RuntimeError, match="livelock"):
        loop.run_until_idle(max_steps=1_000)


def test_event_loop_reschedule_advancing_time_terminates() -> None:
    loop = EventLoop(SimClock())
    fired: list[int] = []

    def _tick(t: int) -> None:
        fired.append(t)
        if t < 500:
            loop.call_after(100, _tick)

    loop.call_at(0, _tick)
    loop.run_until_idle()
    assert fired == [0, 100, 200, 300, 400, 500]
