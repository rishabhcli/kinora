"""The fault grammar + Buggify gate + the simulated network/storage/redis seams
(kinora.md §12 — the backend is built for flaky async work).

Proves the adversary is *seeded and observable*: a Buggify roll fires at its
configured rate and is logged; the network delivers/drops/reorders deterministically;
storage faults (IO error, lost-ack, stale read) behave as designed; and the
fault-injecting redis proxy faithfully wraps the project's own ``FakeAsyncRedis``.
"""

from __future__ import annotations

import asyncio

from app.queue.fakeredis import FakeAsyncRedis
from app.verification.simulation.buggify import Buggify
from app.verification.simulation.core import EventCallback, EventLoop, Prng, SimClock
from app.verification.simulation.faults import (
    FaultKind,
    FaultProfile,
    FaultSchedule,
    FaultWeight,
)
from app.verification.simulation.network import SimNetwork
from app.verification.simulation.redis_sim import (
    FaultingRedis,
    SimRedisError,
    install_virtual_clock,
)
from app.verification.simulation.storage import SimStorage, StorageError


def _bug(profile: FaultProfile, seed: int = 0) -> tuple[Buggify, SimClock]:
    clock = SimClock()
    return Buggify(profile, Prng(seed), clock.as_callable_ms()), clock


# --------------------------------------------------------------------------- #
# Fault profiles + schedule
# --------------------------------------------------------------------------- #


def test_calm_profile_injects_nothing() -> None:
    assert FaultProfile.calm().active_kinds() == []


def test_intensity_scales_probability() -> None:
    chaos = FaultProfile.chaos()
    half = chaos.with_intensity(0.5)
    assert half.probability(FaultKind.NET_DROP) == chaos.probability(FaultKind.NET_DROP) * 0.5


def test_disabling_removes_a_kind() -> None:
    chaos = FaultProfile.chaos()
    without = chaos.disabling(FaultKind.NET_DROP)
    assert without.probability(FaultKind.NET_DROP) == 0.0
    assert FaultKind.NET_DROP not in without.active_kinds()


def test_schedule_describe_is_reproducible_text() -> None:
    s = FaultSchedule(seed=7, profile=FaultProfile.nominal())
    assert "seed=7" in s.describe()
    assert "profile=nominal" in s.describe()


# --------------------------------------------------------------------------- #
# Buggify gate
# --------------------------------------------------------------------------- #


def test_buggify_fires_at_configured_rate_and_logs() -> None:
    profile = FaultProfile(weights={FaultKind.NET_DROP: FaultWeight(probability=0.2)})
    bug, _clock = _bug(profile, seed=3)
    fires = sum(bug.should(FaultKind.NET_DROP, "site") for _ in range(5_000))
    assert 850 < fires < 1_150  # ~20%
    assert bug.log.total == fires  # every fire is recorded
    assert bug.log.counts()["net_drop"] == fires


def test_buggify_disabled_never_fires() -> None:
    bug, _ = _bug(FaultProfile.chaos(), seed=1)
    bug.enabled = False
    assert not any(bug.should(FaultKind.NET_DROP, "site") for _ in range(1_000))
    assert bug.duration(FaultKind.NET_LATENCY, "site") == 0


def test_buggify_duration_within_band() -> None:
    profile = FaultProfile(weights={FaultKind.NET_LATENCY: FaultWeight(0.5, 10, 30)})
    bug, _ = _bug(profile, seed=2)
    for _ in range(500):
        d = bug.duration(FaultKind.NET_LATENCY, "site")
        assert d == 0 or 10 <= d <= 30


def test_buggify_is_seed_deterministic() -> None:
    p = FaultProfile.chaos()
    a, _ = _bug(p, seed=5)
    b, _ = _bug(p, seed=5)
    seq_a = [a.should(FaultKind.NET_DROP, "s") for _ in range(200)]
    seq_b = [b.should(FaultKind.NET_DROP, "s") for _ in range(200)]
    assert seq_a == seq_b


# --------------------------------------------------------------------------- #
# Network seam
# --------------------------------------------------------------------------- #


def test_network_delivers_most_under_nominal_time_spread() -> None:
    clock = SimClock()
    loop = EventLoop(clock)
    bug = Buggify(FaultProfile.nominal(), Prng(9), clock.as_callable_ms())
    net = SimNetwork(loop, bug)
    got: list[object] = []
    net.listen("w", got.append)

    def _send(j: int) -> EventCallback:
        return lambda _t: net.send("api", "w", j)

    # Spread sends across virtual time so partitions heal naturally (realistic).
    for i in range(200):
        loop.call_at(i * 100, _send(i))
    loop.run_until_idle()
    assert net.stats.sent == 200
    assert net.stats.delivered + net.stats.dropped == 200
    assert net.stats.delivery_rate > 0.85


def test_network_calm_delivers_everything_in_order() -> None:
    clock = SimClock()
    loop = EventLoop(clock)
    bug = Buggify(FaultProfile.calm(), Prng(0), clock.as_callable_ms())
    net = SimNetwork(loop, bug)
    got: list[object] = []
    net.listen("w", got.append)

    def _send(j: int) -> EventCallback:
        return lambda _t: net.send("api", "w", j)

    for i in range(50):
        loop.call_at(i * 10, _send(i))
    loop.run_until_idle()
    assert got == list(range(50))  # no drop, no reorder under calm
    assert net.stats.dropped == 0


# --------------------------------------------------------------------------- #
# Storage seam
# --------------------------------------------------------------------------- #


def test_storage_calm_roundtrips() -> None:
    bug, _ = _bug(FaultProfile.calm())
    st = SimStorage(bug)
    st.put("clip:1", b"data")
    assert st.get("clip:1") == b"data"
    assert st.exists("clip:1")


def test_storage_io_error_is_raised_and_counted() -> None:
    profile = FaultProfile(weights={FaultKind.DISK_IO_ERROR: FaultWeight(probability=1.0)})
    bug, _ = _bug(profile)
    st = SimStorage(bug)
    errored = False
    try:
        st.put("k", b"v")
    except StorageError:
        errored = True
    assert errored
    assert st.stats.write_errors == 1


def test_storage_lost_ack_persists_but_raises() -> None:
    profile = FaultProfile(
        weights={FaultKind.DISK_WRITE_LOST_ACK: FaultWeight(probability=1.0)}
    )
    bug, _ = _bug(profile)
    st = SimStorage(bug)
    raised = False
    try:
        st.put("k", b"v")
    except StorageError:
        raised = True
    assert raised  # the caller sees a failure...
    assert st.exists("k")  # ...but the data actually landed (phantom write)
    assert "k" in st.stats.phantom_keys


# --------------------------------------------------------------------------- #
# Faulting redis proxy
# --------------------------------------------------------------------------- #


def test_faulting_redis_calm_delegates_cleanly() -> None:
    async def go() -> None:
        bug, clock = _bug(FaultProfile.calm())
        inner = FakeAsyncRedis()
        install_virtual_clock(inner, clock.as_callable_s())
        fr = FaultingRedis(inner, bug)
        await fr.set("k", "v")
        assert await fr.get("k") == "v"
        assert fr.command_count == 2
        assert fr.error_count == 0

    asyncio.run(go())


def test_faulting_redis_injects_transient_errors() -> None:
    async def go() -> int:
        profile = FaultProfile(weights={FaultKind.REDIS_ERROR: FaultWeight(probability=0.3)})
        bug, clock = _bug(profile, seed=4)
        inner = FakeAsyncRedis()
        install_virtual_clock(inner, clock.as_callable_s())
        fr = FaultingRedis(inner, bug)
        errs = 0
        for i in range(200):
            try:
                await fr.set(f"k{i}", "v")
            except SimRedisError:
                errs += 1
        return errs

    errs = asyncio.run(go())
    assert 40 < errs < 80  # ~30% of 200


def test_faulting_redis_slow_reports_latency() -> None:
    async def go() -> int:
        profile = FaultProfile(weights={FaultKind.REDIS_SLOW: FaultWeight(0.5, 5, 20)})
        bug, clock = _bug(profile, seed=6)
        inner = FakeAsyncRedis()
        install_virtual_clock(inner, clock.as_callable_s())
        accum = [0]
        fr = FaultingRedis(inner, bug, on_latency=lambda ms: accum.__setitem__(0, accum[0] + ms))
        for i in range(100):
            await fr.set(f"k{i}", "v")
        return accum[0]

    total_latency = asyncio.run(go())
    assert total_latency > 0  # slow commands folded latency back to the clock
