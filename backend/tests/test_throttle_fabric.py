"""Deterministic tests for the distributed rate-limit fabric (:mod:`app.throttle`).

No infra, no network, no real sleeps: every test wires a :class:`ManualClock` +
the :class:`InMemoryScriptTransport` emulator so the limiters are pure functions
of (state, args, time). Coverage:

* each algorithm's admit/deny correctness, refill/leak math, and retry-after
  accuracy (token bucket, sliding-window log, GCRA);
* the in-memory emulator's atomicity (concurrent acquires serialise);
* hierarchical most-restrictive-wins enforcement *and* the all-or-nothing
  rollback that makes a denied acquire side-effect-free;
* distributed concurrency leases: the fleet cap, crash reclaim via TTL, renew,
  and release;
* fairness across callers (the round-robin shape of a shared limiter);
* quota refund (the reservation give-back path);
* the client: try_acquire vs blocking acquire, precise Retry-After, and
  fail-open / fail-closed on a downed store;
* the declarative :func:`build_client` factory.
"""

from __future__ import annotations

import asyncio

import pytest

from app.throttle import (
    ConcurrencyLeasePool,
    FabricSpec,
    GcraConfig,
    GcraLimiter,
    HierarchicalLimiter,
    InMemoryScriptTransport,
    InMemoryStore,
    LeaseConfig,
    LeaseSpec,
    LeaseUnavailable,
    Level,
    LimitSpec,
    ManualClock,
    SlidingWindowConfig,
    SlidingWindowLimiter,
    StoreUnavailable,
    ThrottleClient,
    Throttled,
    TokenBucketConfig,
    TokenBucketLimiter,
    build_client,
)
from app.throttle.hierarchy import GcraLimit, SlidingWindowLimit, TokenBucketLimit

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def make_transport(
    start: float = 0.0,
) -> tuple[ManualClock, InMemoryStore, InMemoryScriptTransport]:
    clock = ManualClock(start)
    store = InMemoryStore(clock)
    transport = InMemoryScriptTransport(store, clock=clock)
    return clock, store, transport


# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #


async def test_token_bucket_admits_burst_then_denies() -> None:
    clock, _store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=2.0, capacity=5.0))

    # Full bucket admits a burst of 5.
    for _ in range(5):
        assert (await tb.check()).allowed
    # The 6th is denied with a precise wait: 1 token / 2 per sec = 0.5 s.
    d = await tb.check()
    assert not d.allowed
    assert d.retry_after == pytest.approx(0.5)


async def test_token_bucket_refills_continuously() -> None:
    clock, _store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=4.0, capacity=4.0))
    for _ in range(4):
        assert (await tb.check()).allowed
    assert not (await tb.check()).allowed

    # After 0.25 s at 4/s exactly one token returns -> one admit, then deny.
    clock.advance(0.25)
    assert (await tb.check()).allowed
    assert not (await tb.check()).allowed


async def test_token_bucket_caps_refill_at_capacity() -> None:
    clock, store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=10.0, capacity=3.0))
    assert (await tb.check()).allowed  # create state, consume 1 (-> 2 left)
    clock.advance(100.0)  # would refill 1000 tokens, but capacity clamps to 3
    for _ in range(3):
        assert (await tb.check()).allowed
    assert not (await tb.check()).allowed


async def test_token_bucket_cost_greater_than_one() -> None:
    _clock, _store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=1.0, capacity=10.0))
    assert (await tb.check(cost=7)).allowed
    d = await tb.check(cost=7)  # only 3 left, deficit 4 / 1 per s = 4 s
    assert not d.allowed
    assert d.retry_after == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# Sliding-window log
# --------------------------------------------------------------------------- #


async def test_sliding_window_exact_count() -> None:
    clock, _store, t = make_transport()
    sw = SlidingWindowLimiter(t, "q", SlidingWindowConfig(limit=3, window_s=1.0))
    for _ in range(3):
        assert (await sw.check()).allowed
    d = await sw.check()
    assert not d.allowed
    # The oldest entry is at t=0; it falls out at t=1, so wait ~1 s.
    assert d.retry_after == pytest.approx(1.0)


async def test_sliding_window_slides_not_fixed() -> None:
    """A sliding window must not let 2*limit through across a boundary."""
    clock, _store, t = make_transport()
    sw = SlidingWindowLimiter(t, "q", SlidingWindowConfig(limit=2, window_s=1.0))
    assert (await sw.check()).allowed  # t=0
    clock.advance(0.6)
    assert (await sw.check()).allowed  # t=0.6 ; 2 in window
    clock.advance(0.5)  # t=1.1: the t=0 entry just expired, t=0.6 still in
    assert (await sw.check()).allowed  # admits (1 in window + this)
    # Now t=0.6 and t=1.1 are in window -> next denied.
    assert not (await sw.check()).allowed


async def test_sliding_window_retry_after_tracks_oldest() -> None:
    clock, _store, t = make_transport()
    sw = SlidingWindowLimiter(t, "q", SlidingWindowConfig(limit=1, window_s=2.0))
    assert (await sw.check()).allowed  # t=0
    clock.advance(0.5)
    d = await sw.check()  # t=0.5, oldest at 0 -> falls out at 2 -> wait 1.5
    assert not d.allowed
    assert d.retry_after == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# GCRA / leaky bucket
# --------------------------------------------------------------------------- #


async def test_gcra_paces_at_emission_interval() -> None:
    clock, _store, t = make_transport()
    g = GcraLimiter(t, "r", GcraConfig(rate=2.0, burst=1))  # T=0.5, no burst
    assert (await g.check()).allowed
    d = await g.check()
    assert not d.allowed
    assert d.retry_after == pytest.approx(0.5)
    clock.advance(0.5)
    assert (await g.check()).allowed


async def test_gcra_allows_burst() -> None:
    clock, _store, t = make_transport()
    g = GcraLimiter(t, "r", GcraConfig(rate=4.0, burst=3))  # T=0.25, tau=0.5
    for _ in range(3):
        assert (await g.check()).allowed
    d = await g.check()
    assert not d.allowed
    # 4th conforms once now >= tat - tau. tat=0.75 after 3 admits, tau=0.5 -> 0.25.
    assert d.retry_after == pytest.approx(0.25)


async def test_gcra_equivalent_to_token_bucket_dual() -> None:
    """GCRA(T=1/R, tau=(B-1)T) admits the same burst as a token bucket(R, B)."""
    _clock, _store, t = make_transport()
    g = GcraLimiter(t, "g", GcraConfig(rate=5.0, burst=4))
    admits = 0
    for _ in range(10):
        if (await g.check()).allowed:
            admits += 1
    assert admits == 4  # exactly the burst, no refill (time frozen)


# --------------------------------------------------------------------------- #
# Emulator atomicity
# --------------------------------------------------------------------------- #


async def test_emulator_serialises_concurrent_acquires() -> None:
    """Concurrent acquires must not over-admit (the emulator's lock = redis atomicity)."""
    _clock, _store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=1.0, capacity=5.0))
    results = await asyncio.gather(*[tb.check() for _ in range(20)])
    admitted = sum(1 for r in results if r.allowed)
    assert admitted == 5  # never 6+, despite 20 racing


# --------------------------------------------------------------------------- #
# Hierarchy: most-restrictive wins + rollback
# --------------------------------------------------------------------------- #


def _hierarchy(t: InMemoryScriptTransport) -> HierarchicalLimiter:
    return HierarchicalLimiter(
        [
            TokenBucketLimit(
                TokenBucketLimiter(t, "global", TokenBucketConfig(rate=100, capacity=100))
            ),
            SlidingWindowLimit(
                SlidingWindowLimiter(t, "provider", SlidingWindowConfig(limit=2, window_s=1.0))
            ),
        ]
    )


async def test_hierarchy_most_restrictive_binds() -> None:
    clock, _store, t = make_transport()
    h = _hierarchy(t)
    assert (await h.acquire()).allowed
    assert (await h.acquire()).allowed
    d = await h.acquire()  # provider sliding-window (2/s) binds, not global (100)
    assert not d.allowed
    assert "provider" in d.binding
    assert d.retry_after == pytest.approx(1.0)


async def test_hierarchy_rolls_back_admitted_levels_on_deny() -> None:
    """When a lower level denies, the higher level must be refunded (no leak)."""
    clock, store, t = make_transport()
    h = _hierarchy(t)
    await h.acquire()
    await h.acquire()
    # Global bucket has consumed exactly 2 so far.
    tokens_before = store.hgetall("throttle:tb:global")["tokens"]
    assert tokens_before == pytest.approx(98.0)

    # This acquire is denied by the provider level; global must be refunded.
    d = await h.acquire()
    assert not d.allowed
    tokens_after = store.hgetall("throttle:tb:global")["tokens"]
    assert tokens_after == pytest.approx(98.0)  # NOT 97 — the deny was rolled back


async def test_hierarchy_strict_max_reports_worst_wait() -> None:
    clock, _store, t = make_transport()
    h = HierarchicalLimiter(
        [
            GcraLimit(GcraLimiter(t, "a", GcraConfig(rate=10.0, burst=1))),  # wait 0.1
            GcraLimit(GcraLimiter(t, "b", GcraConfig(rate=1.0, burst=1))),  # wait 1.0
        ]
    )
    assert (await h.acquire_strict_max()).allowed
    d = await h.acquire_strict_max()
    assert not d.allowed
    # strict_max probes both; the worst (slowest) level dictates the back-off.
    assert d.retry_after == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Concurrency leases
# --------------------------------------------------------------------------- #


def _seeded_pool(t: InMemoryScriptTransport, capacity: int, ttl: float) -> ConcurrencyLeasePool:
    ids = iter(range(1, 10_000))
    return ConcurrencyLeasePool(
        t, "wan", LeaseConfig(capacity=capacity, ttl_s=ttl), id_factory=lambda: next(ids)
    )


async def test_lease_caps_fleet_concurrency() -> None:
    _clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=2, ttl=30.0)
    l1 = await pool.try_acquire()
    l2 = await pool.try_acquire()
    l3 = await pool.try_acquire()
    assert l1 is not None and l2 is not None and l3 is None
    assert await pool.in_flight() == 2


async def test_lease_release_frees_a_slot() -> None:
    _clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=1, ttl=30.0)
    l1 = await pool.try_acquire()
    assert l1 is not None
    assert await pool.try_acquire() is None
    await l1.release()
    assert await pool.try_acquire() is not None


async def test_lease_ttl_reclaims_crashed_holder() -> None:
    """A holder that never releases (crash) has its slot reclaimed after the TTL."""
    clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=1, ttl=10.0)
    l1 = await pool.try_acquire()
    assert l1 is not None
    assert await pool.try_acquire() is None  # full
    clock.advance(10.001)  # l1's lease expires (holder "crashed")
    assert await pool.try_acquire() is not None  # reclaimed


async def test_lease_renew_extends_and_blocks_reclaim() -> None:
    clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=1, ttl=10.0)
    lease = pool.lease()
    assert await lease.acquire()
    clock.advance(8.0)
    assert await lease.renew()  # heartbeat before expiry
    clock.advance(8.0)  # 16 s since acquire, but only 8 since renew -> still held
    assert await pool.try_acquire() is None


async def test_lease_renew_fails_after_expiry() -> None:
    clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=1, ttl=5.0)
    lease = pool.lease()
    assert await lease.acquire()
    clock.advance(5.001)  # expired
    assert not await lease.renew()
    assert not lease.held


async def test_lease_context_manager_raises_when_full() -> None:
    _clock, _store, t = make_transport()
    pool = _seeded_pool(t, capacity=1, ttl=30.0)
    held = pool.lease()
    async with held:
        with pytest.raises(LeaseUnavailable) as ei:
            async with pool.lease():
                pass
        assert ei.value.capacity == 1
        assert ei.value.in_flight == 1
    # The first lease released on exit -> a slot is free again.
    assert await pool.try_acquire() is not None


# --------------------------------------------------------------------------- #
# Fairness across callers
# --------------------------------------------------------------------------- #


async def test_fairness_shared_limiter_serves_callers_in_order() -> None:
    """Two callers sharing one limiter each get their fair share as it refills.

    With one token per 1 s and two callers polling, admissions alternate over
    time rather than one caller starving the other — the shared-state limiter is
    caller-agnostic, so order of arrival decides each tick.
    """
    clock, _store, t = make_transport()
    tb = TokenBucketLimiter(t, "shared", TokenBucketConfig(rate=1.0, capacity=1.0))

    # t=0: one token. Caller A wins it.
    assert (await tb.check()).allowed  # A
    assert not (await tb.check()).allowed  # B denied
    # t=1: one token refills. Now B arrives first and wins.
    clock.advance(1.0)
    assert (await tb.check()).allowed  # B
    assert not (await tb.check()).allowed  # A denied
    # Over two ticks each caller got exactly one — fair.


# --------------------------------------------------------------------------- #
# Quota refund (reservation give-back)
# --------------------------------------------------------------------------- #


async def test_token_bucket_refund_returns_tokens() -> None:
    _clock, store, t = make_transport()
    tb = TokenBucketLimiter(t, "p", TokenBucketConfig(rate=1.0, capacity=5.0))
    for _ in range(5):
        assert (await tb.check()).allowed
    assert not (await tb.check()).allowed
    await tb.refund(2.0)  # a reservation of 2 went unused
    assert (await tb.check()).allowed
    assert (await tb.check()).allowed
    assert not (await tb.check()).allowed  # only the 2 refunded came back


async def test_gcra_refund_rewinds_tat() -> None:
    _clock, _store, t = make_transport()
    g = GcraLimiter(t, "r", GcraConfig(rate=2.0, burst=1))
    assert (await g.check()).allowed
    assert not (await g.check()).allowed
    await g.refund(1.0)
    assert (await g.check()).allowed  # the slot came back


async def test_sliding_window_refund_removes_members() -> None:
    _clock, _store, t = make_transport()
    sw = SlidingWindowLimiter(t, "q", SlidingWindowConfig(limit=1, window_s=10.0))
    d, seed = await sw._check_with_seed(1)
    assert d.allowed
    assert not (await sw.check()).allowed
    await sw.refund(1, seed)
    assert (await sw.check()).allowed


# --------------------------------------------------------------------------- #
# Client: try_acquire / acquire / Retry-After / fail-open
# --------------------------------------------------------------------------- #


async def test_client_try_acquire_reports_retry_after() -> None:
    _clock, _store, t = make_transport()
    h = HierarchicalLimiter(
        [GcraLimit(GcraLimiter(t, "p", GcraConfig(rate=2.0, burst=1)))]
    )
    client = ThrottleClient(h)
    assert (await client.try_acquire()).allowed
    v = await client.try_acquire()
    assert not v.allowed
    assert v.retry_after == pytest.approx(0.5)
    assert v.retry_after_seconds_ceil() == 1  # header rounds up


async def test_client_blocking_acquire_waits_then_admits() -> None:
    """Blocking acquire sleeps exactly retry_after (advancing the clock) and retries."""
    clock, _store, t = make_transport()
    h = HierarchicalLimiter(
        [GcraLimit(GcraLimiter(t, "p", GcraConfig(rate=4.0, burst=1)))]
    )
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock.advance(s)  # the wait advances the (shared) server clock

    client = ThrottleClient(h, clock=clock, sleep=fake_sleep)
    assert (await client.acquire()).allowed  # immediate
    v = await client.acquire()  # must wait one emission interval then admit
    assert v.allowed
    assert slept == [pytest.approx(0.25)]


async def test_client_blocking_acquire_honours_deadline() -> None:
    clock, _store, t = make_transport()
    h = HierarchicalLimiter(
        [GcraLimit(GcraLimiter(t, "p", GcraConfig(rate=1.0, burst=1)))]
    )

    async def fake_sleep(s: float) -> None:
        clock.advance(s)

    client = ThrottleClient(h, clock=clock, sleep=fake_sleep)
    assert (await client.acquire()).allowed
    # Next admit needs 1 s but max_wait is 0.5 -> raises Throttled.
    with pytest.raises(Throttled) as ei:
        await client.acquire(max_wait=0.5)
    assert ei.value.retry_after == pytest.approx(1.0)


class _BrokenTransport:
    """A transport that always fails — to exercise fail-open / fail-closed."""

    async def run(self, unit: object, keys: list[str], args: list[float]) -> list[float]:
        raise StoreUnavailable("redis down")

    async def server_time(self) -> float:
        raise StoreUnavailable("redis down")


async def test_client_fail_open_admits_when_store_down() -> None:
    broken = _BrokenTransport()
    h = HierarchicalLimiter([GcraLimit(GcraLimiter(broken, "p", GcraConfig(rate=1.0)))])
    client = ThrottleClient(h, fail_open=True)
    v = await client.try_acquire()
    assert v.allowed
    assert v.fail_open is True


async def test_client_fail_closed_denies_when_store_down() -> None:
    broken = _BrokenTransport()
    h = HierarchicalLimiter([GcraLimit(GcraLimiter(broken, "p", GcraConfig(rate=1.0)))])
    client = ThrottleClient(h, fail_open=False)
    v = await client.try_acquire()
    assert not v.allowed
    assert v.scope == "store"
    assert v.retry_after == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# build_client factory + guard (rate + concurrency together)
# --------------------------------------------------------------------------- #


async def test_build_client_enforces_full_hierarchy_and_lease() -> None:
    clock, _store, t = make_transport()
    spec = FabricSpec(
        limits=[
            LimitSpec(
                algorithm="token_bucket", level=Level.GLOBAL, scope="all", rate=100, capacity=100
            ),
            LimitSpec(
                algorithm="sliding_window",
                level=Level.PROVIDER,
                scope="dashscope",
                limit=3,
                window_s=1.0,
            ),
        ],
        lease=LeaseSpec(scope="wan", capacity=2, ttl_s=30.0),
        fail_open=True,
    )
    client = build_client(spec, t, clock=clock)
    res = [await client.try_acquire() for _ in range(3)]
    assert all(r.allowed for r in res)
    v = await client.try_acquire()  # provider limit (3/s) binds
    assert not v.allowed
    assert "dashscope" in v.scope


async def test_build_client_sorts_levels_broad_first() -> None:
    """Specs given out of order are enforced broad→narrow regardless."""
    _clock, _store, t = make_transport()
    spec = FabricSpec(
        limits=[
            LimitSpec(algorithm="gcra", level=Level.ENDPOINT, scope="image", rate=100),
            LimitSpec(algorithm="gcra", level=Level.GLOBAL, scope="all", rate=1.0),
        ]
    )
    client = build_client(spec, t)
    assert (await client.try_acquire()).allowed
    v = await client.try_acquire()  # global (1/s) is the bottleneck
    assert not v.allowed
    assert "global" in v.scope


async def test_guard_acquires_rate_and_lease_then_releases() -> None:
    clock, _store, t = make_transport()
    spec = FabricSpec(
        limits=[
            LimitSpec(
                algorithm="token_bucket",
                level=Level.GLOBAL,
                scope="all",
                rate=100,
                capacity=100,
            )
        ],
        lease=LeaseSpec(scope="wan", capacity=1, ttl_s=30.0),
    )

    async def fake_sleep(s: float) -> None:
        clock.advance(s)

    client = build_client(spec, t, clock=clock, sleep=fake_sleep)
    assert client._lease_pool is not None
    async with client.guard() as lease:
        assert lease is not None and lease.held
        assert await client._lease_pool.in_flight() == 1
    # Lease released on exit.
    assert await client._lease_pool.in_flight() == 0


async def test_guard_raises_lease_unavailable_when_pool_full() -> None:
    clock, _store, t = make_transport()
    spec = FabricSpec(
        limits=[
            LimitSpec(
                algorithm="token_bucket",
                level=Level.GLOBAL,
                scope="all",
                rate=100,
                capacity=100,
            )
        ],
        lease=LeaseSpec(scope="wan", capacity=1, ttl_s=30.0),
    )

    async def fake_sleep(s: float) -> None:
        clock.advance(s)

    client = build_client(spec, t, clock=clock, sleep=fake_sleep)
    async with client.guard():
        with pytest.raises(LeaseUnavailable):
            async with client.guard():
                pass


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #


def test_limitspec_rejects_missing_params() -> None:
    with pytest.raises(ValueError, match="token_bucket needs"):
        LimitSpec(algorithm="token_bucket", level=Level.GLOBAL, scope="x", rate=1.0)
    with pytest.raises(ValueError, match="sliding_window needs"):
        LimitSpec(algorithm="sliding_window", level=Level.GLOBAL, scope="x", limit=5)
    with pytest.raises(ValueError, match="gcra needs"):
        LimitSpec(algorithm="gcra", level=Level.GLOBAL, scope="x")


def test_config_validators_reject_bad_values() -> None:
    with pytest.raises(ValueError):
        TokenBucketConfig(rate=0, capacity=1)
    with pytest.raises(ValueError):
        SlidingWindowConfig(limit=0, window_s=1)
    with pytest.raises(ValueError):
        GcraConfig(rate=-1)
    with pytest.raises(ValueError):
        LeaseConfig(capacity=0, ttl_s=1)
