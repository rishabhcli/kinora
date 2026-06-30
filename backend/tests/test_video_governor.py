"""Deterministic unit tests for the video provider SLA/quota governor.

Everything here runs with **no infra and no network**: a :class:`FakeClock` drives
all time, the :class:`InMemoryGovernorStore` backs all counters, and outcomes are
scripted. No test enables ``KINORA_LIVE_VIDEO`` or spends. Covered:

* windowed quota accounting + rollover (rpm / daily seconds / monthly spend),
* concurrency gauge reserve/release (no negative drift),
* throttle pacing under a burst, and Retry-After / fallback backoff + recovery,
* fair-share weighting and anti-starvation across tenants,
* error-budget burn + A–F grading,
* capacity-oracle answers + cross-provider ranking,
* the governor admit→complete lease lifecycle and the emitted events.
"""

from __future__ import annotations

import asyncio

import pytest

from app.video.governor import (
    CapacityOracle,
    DenyReason,
    EventCode,
    FairShareAllocator,
    FairShareConfig,
    FakeClock,
    GovernorConfig,
    GovernorEventBus,
    InMemoryGovernorStore,
    ProviderGovernor,
    ProviderProfile,
    ProviderThrottle,
    QuotaAccountant,
    QuotaDimension,
    QuotaLimits,
    RenderCost,
    Severity,
    SlaGrade,
    SlaObjective,
    SlaTracker,
    ThrottleConfig,
    best_provider,
    default_video_profiles,
    window_start,
)

# --------------------------------------------------------------------------- #
# window math
# --------------------------------------------------------------------------- #


def test_window_start_aligns_to_tumbling_boundary() -> None:
    assert window_start(0.0, 60) == 0
    assert window_start(59.9, 60) == 0
    assert window_start(60.0, 60) == 60
    assert window_start(125.0, 60) == 120
    assert window_start(86_400 + 5, 86_400) == 86_400


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


async def test_store_window_incr_and_gauge_floor() -> None:
    store = InMemoryGovernorStore()
    assert await store.incr_window("k", 0, 2.0, ttl_s=120) == 2.0
    assert await store.incr_window("k", 0, 3.0, ttl_s=120) == 5.0
    assert await store.read_window("k", 0) == 5.0
    assert await store.read_window("k", 60) == 0.0  # different bucket

    assert await store.adjust_gauge("g", 2) == 2
    assert await store.adjust_gauge("g", -5) == 0  # floored, never negative
    assert await store.read_gauge("g") == 0


async def test_store_expire_before_reclaims_stale_buckets() -> None:
    store = InMemoryGovernorStore()
    await store.incr_window("k", 0, 1.0, ttl_s=120)  # expires at 120
    await store.incr_window("k", 200, 1.0, ttl_s=120)  # expires at 320
    removed = store.expire_before(150)
    assert removed == 1
    assert await store.read_window("k", 0) == 0.0
    assert await store.read_window("k", 200) == 1.0


# --------------------------------------------------------------------------- #
# quota accounting + rollover
# --------------------------------------------------------------------------- #


def _accountant(
    limits: QuotaLimits, clock: FakeClock
) -> tuple[QuotaAccountant, InMemoryGovernorStore]:
    store = InMemoryGovernorStore()
    return QuotaAccountant("p", limits, store, clock=clock), store


async def test_quota_requests_per_min_blocks_then_rolls_over() -> None:
    clock = FakeClock()
    acct, _ = _accountant(QuotaLimits(requests_per_min=2), clock)
    cost = RenderCost(requests=1, concurrent=0)

    assert (await acct.reserve(cost)).admitted is True
    assert (await acct.reserve(cost)).admitted is True
    third = await acct.reserve(cost)
    assert third.admitted is False
    blocking = third.blocking
    assert len(blocking) == 1
    assert blocking[0].dimension is QuotaDimension.REQUESTS_PER_MIN

    # Next minute: the tumbling window rolls over and admits again.
    clock.advance(60.0)
    assert (await acct.reserve(cost)).admitted is True


async def test_quota_daily_video_seconds_and_monthly_spend() -> None:
    clock = FakeClock()
    acct, _ = _accountant(
        QuotaLimits(daily_video_seconds=10.0, monthly_spend_usd=1.0), clock
    )
    assert (await acct.reserve(RenderCost(video_seconds=6.0, spend_usd=0.4))).admitted
    # 6 + 6 > 10 daily → refused on the daily-seconds axis.
    decision = await acct.reserve(RenderCost(video_seconds=6.0, spend_usd=0.4))
    assert decision.admitted is False
    assert {u.dimension for u in decision.blocking} == {QuotaDimension.DAILY_VIDEO_SECONDS}

    # A small render fits the day but would blow the monthly spend (0.4 + 0.4 + 0.4 > 1.0).
    assert (await acct.reserve(RenderCost(video_seconds=1.0, spend_usd=0.4))).admitted
    spend_block = await acct.reserve(RenderCost(video_seconds=1.0, spend_usd=0.4))
    assert spend_block.admitted is False
    assert {u.dimension for u in spend_block.blocking} == {QuotaDimension.MONTHLY_SPEND_USD}

    # Next day the seconds window rolls; spend (monthly) does not.
    clock.advance(86_400.0)
    day = await acct.reserve(RenderCost(video_seconds=9.0, spend_usd=0.0))
    assert day.admitted is True


async def test_quota_concurrency_gauge_reserve_and_release() -> None:
    clock = FakeClock()
    acct, _ = _accountant(QuotaLimits(concurrent_jobs=2), clock)
    cost = RenderCost(requests=0, concurrent=1)

    assert (await acct.reserve(cost)).admitted
    assert (await acct.reserve(cost)).admitted
    assert (await acct.reserve(cost)).admitted is False  # 2 in flight
    await acct.release(1)
    assert (await acct.reserve(cost)).admitted is True  # a slot freed


async def test_quota_unbounded_dimension_never_blocks() -> None:
    clock = FakeClock()
    acct, _ = _accountant(QuotaLimits(), clock)  # everything unbounded
    for _ in range(50):
        assert (await acct.reserve(RenderCost(video_seconds=100.0, spend_usd=100.0))).admitted


async def test_quota_refusal_records_nothing() -> None:
    clock = FakeClock()
    acct, _ = _accountant(QuotaLimits(daily_video_seconds=5.0), clock)
    assert (await acct.reserve(RenderCost(video_seconds=6.0))).admitted is False
    # A subsequent fitting render still has the full budget — nothing was charged.
    assert (await acct.reserve(RenderCost(video_seconds=5.0))).admitted is True


async def test_quota_near_limit_reports_highest_crossed_fraction() -> None:
    clock = FakeClock()
    acct, _ = _accountant(
        QuotaLimits(daily_video_seconds=100.0, alert_fractions=(0.5, 0.9)), clock
    )
    await acct.reserve(RenderCost(video_seconds=95.0))
    hits = list(await acct.near_limit())
    assert len(hits) == 1
    usage, frac = hits[0]
    assert usage.dimension is QuotaDimension.DAILY_VIDEO_SECONDS
    assert frac == 0.9  # the highest fraction crossed, reported once


# --------------------------------------------------------------------------- #
# throttle pacing + backoff
# --------------------------------------------------------------------------- #


def test_throttle_burst_then_paces() -> None:
    clock = FakeClock()
    # 60/min ⇒ 1s gap; burst of 3 goes immediately, then paces.
    thr = ProviderThrottle("p", ThrottleConfig(rate_per_min=60.0, burst=3), clock=clock)
    assert thr.acquire_delay() == 0.0
    assert thr.acquire_delay() == 0.0
    assert thr.acquire_delay() == 0.0
    # Burst spent: the 4th must wait ~1s.
    wait = thr.acquire_delay()
    assert wait == pytest.approx(1.0, abs=1e-6)
    # Advance past the gap and it opens.
    clock.advance(1.0)
    assert thr.acquire_delay() == 0.0


def test_throttle_unpaced_when_rate_zero() -> None:
    clock = FakeClock()
    thr = ProviderThrottle("p", ThrottleConfig(rate_per_min=0.0, burst=1), clock=clock)
    for _ in range(100):
        assert thr.acquire_delay() == 0.0


def test_throttle_retry_after_parks_all_submissions() -> None:
    clock = FakeClock()
    thr = ProviderThrottle("p", ThrottleConfig(rate_per_min=600.0, burst=5), clock=clock)
    backoff = thr.note_rate_limited(retry_after_s=30.0)
    assert backoff == 30.0
    assert thr.is_backed_off() is True
    # Even with burst budget, nothing goes out until Retry-After elapses.
    assert thr.acquire_delay() == pytest.approx(30.0, abs=1e-6)
    clock.advance(29.0)
    assert thr.acquire_delay() == pytest.approx(1.0, abs=1e-6)
    clock.advance(1.0)
    assert thr.is_backed_off() is False
    assert thr.acquire_delay() == 0.0


def test_throttle_fallback_backoff_grows_and_caps() -> None:
    clock = FakeClock()
    thr = ProviderThrottle(
        "p",
        ThrottleConfig(fallback_backoff_s=2.0, backoff_multiplier=2.0, max_backoff_s=10.0),
        clock=clock,
    )
    assert thr.note_rate_limited() == 2.0  # 2 * 2^0
    clock.advance(2.0)
    assert thr.note_rate_limited() == 4.0  # 2 * 2^1
    clock.advance(4.0)
    assert thr.note_rate_limited() == 8.0  # 2 * 2^2
    clock.advance(8.0)
    assert thr.note_rate_limited() == 10.0  # capped


def test_throttle_overlapping_retry_after_never_shortens() -> None:
    clock = FakeClock()
    thr = ProviderThrottle("p", ThrottleConfig(), clock=clock)
    thr.note_rate_limited(retry_after_s=30.0)
    thr.note_rate_limited(retry_after_s=5.0)  # shorter — must not pull the park in
    assert thr.acquire_delay() == pytest.approx(30.0, abs=1e-6)


def test_throttle_success_signals_recovery_once() -> None:
    clock = FakeClock()
    thr = ProviderThrottle("p", ThrottleConfig(), clock=clock)
    thr.note_rate_limited(retry_after_s=1.0)
    clock.advance(1.0)
    assert thr.note_success() is True  # cleared the backoff
    assert thr.note_success() is False  # already clean


async def test_throttle_async_throttle_advances_fake_clock() -> None:
    clock = FakeClock()

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    thr = ProviderThrottle(
        "p", ThrottleConfig(rate_per_min=60.0, burst=1), clock=clock, sleep=sleep
    )
    assert await thr.throttle() == 0.0  # burst slot
    waited = await thr.throttle()  # paced ~1s, advances the fake clock
    assert waited == pytest.approx(1.0, abs=1e-6)
    assert clock.now == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# fair-share + anti-starvation
# --------------------------------------------------------------------------- #


def test_fairshare_weights_drive_grant_shares() -> None:
    clock = FakeClock()
    alloc = FairShareAllocator(FairShareConfig(), clock=clock)
    alloc.register("big", weight=3.0)
    alloc.register("small", weight=1.0)
    # Both have unbounded demand; grant many slots and check the ~3:1 split.
    for _ in range(40):
        alloc.request("big", 1)
        alloc.request("small", 1)
    grants = [alloc.grant().tenant_id for _ in range(40)]
    big = grants.count("big")
    small = grants.count("small")
    assert big > small
    # Roughly 3:1; allow slack for the discrete deficit rounding.
    assert 2.2 <= big / max(small, 1) <= 4.0


def test_fairshare_anti_starvation_eventually_serves_light_tenant() -> None:
    clock = FakeClock()
    alloc = FairShareAllocator(FairShareConfig(starvation_age_s=10.0), clock=clock)
    alloc.register("whale", weight=100.0)
    alloc.register("minnow", weight=1.0)
    alloc.request("whale", 5)
    alloc.request("minnow", 1)
    # Whale dominates initially.
    assert alloc.grant().tenant_id == "whale"
    assert alloc.is_starving("minnow") is False
    # Time passes; the minnow ages past the starvation threshold and must win next.
    clock.advance(11.0)
    assert alloc.is_starving("minnow") is True
    assert "minnow" in alloc.starving_tenants()
    # The minnow has waited from t=0 (never served) and is owed the most, so even
    # though the heavyweight whale also wants slots, the minnow is granted next.
    decision = alloc.grant()
    assert decision.tenant_id == "minnow"
    assert decision.starving is True


def test_fairshare_no_contenders_returns_none() -> None:
    alloc = FairShareAllocator(clock=FakeClock())
    assert alloc.next_tenant().tenant_id is None
    assert alloc.grant().tenant_id is None


def test_fairshare_withdraw_drops_demand() -> None:
    alloc = FairShareAllocator(clock=FakeClock())
    alloc.request("t", 3)
    alloc.withdraw("t", 2)
    assert alloc.demand("t") == 1
    alloc.withdraw("t", 5)  # clamps at 0
    assert alloc.demand("t") == 0
    assert alloc.next_tenant().tenant_id is None


# --------------------------------------------------------------------------- #
# SLA error-budget + grading
# --------------------------------------------------------------------------- #


def test_sla_fresh_provider_grades_b_until_min_samples() -> None:
    clock = FakeClock()
    sla = SlaTracker("p", SlaObjective(min_samples=5), clock=clock)
    sla.record_success(100.0)
    assert sla.snapshot().grade is SlaGrade.B  # too few samples to judge


def test_sla_all_success_grades_a_zero_burn() -> None:
    clock = FakeClock()
    sla = SlaTracker("p", SlaObjective(min_samples=3, target_success_rate=0.9), clock=clock)
    for _ in range(10):
        sla.record_success(50.0)
    snap = sla.snapshot()
    assert snap.success_rate == 1.0
    assert snap.error_budget_burn == 0.0
    assert snap.grade is SlaGrade.A


def test_sla_error_budget_burn_computation() -> None:
    clock = FakeClock()
    # SLO 90% success ⇒ budget = 10% failures tolerated.
    sla = SlaTracker("p", SlaObjective(min_samples=4, target_success_rate=0.9), clock=clock)
    # 5% observed failure rate burns half the 10% budget.
    for _ in range(19):
        sla.record_success(50.0)
    sla.record_failure(50.0)  # 1/20 = 5% failure
    snap = sla.snapshot()
    assert snap.success_rate == pytest.approx(0.95)
    assert snap.error_budget_burn == pytest.approx(0.5, abs=1e-9)


def test_sla_breach_when_budget_exhausted_grades_f() -> None:
    clock = FakeClock()
    sla = SlaTracker("p", SlaObjective(min_samples=4, target_success_rate=0.9), clock=clock)
    # 20% failures > 10% budget ⇒ burn 2.0 ⇒ F.
    for _ in range(8):
        sla.record_success(10.0)
    for _ in range(2):
        sla.record_failure(10.0)
    snap = sla.snapshot()
    assert snap.error_budget_burn == pytest.approx(2.0, abs=1e-9)
    assert snap.grade is SlaGrade.F
    assert snap.healthy is False


def test_sla_latency_breach_grades_c() -> None:
    clock = FakeClock()
    sla = SlaTracker(
        "p",
        SlaObjective(min_samples=4, target_success_rate=0.5, target_p95_latency_ms=100.0),
        clock=clock,
    )
    for _ in range(20):
        sla.record_success(500.0)  # all succeed but slow
    snap = sla.snapshot()
    assert snap.success_rate == 1.0
    assert snap.latency_breach is True
    assert snap.p95_latency_ms == pytest.approx(500.0)
    assert snap.grade is SlaGrade.C


def test_sla_recovers_as_bad_samples_age_out_of_window() -> None:
    clock = FakeClock()
    sla = SlaTracker(
        "p", SlaObjective(min_samples=4, target_success_rate=0.9, window_size=10), clock=clock
    )
    for _ in range(10):
        sla.record_failure(10.0)
    assert sla.snapshot().grade is SlaGrade.F
    # Push 10 successes through the size-10 ring; the failures fall off.
    for _ in range(10):
        sla.record_success(10.0)
    assert sla.snapshot().grade is SlaGrade.A


def test_sla_zero_budget_objective_any_failure_breaches() -> None:
    clock = FakeClock()
    sla = SlaTracker("p", SlaObjective(min_samples=2, target_success_rate=1.0), clock=clock)
    sla.record_success(10.0)
    sla.record_failure(10.0)
    assert sla.error_budget_burn() >= 1.0
    assert sla.snapshot().grade is SlaGrade.F


# --------------------------------------------------------------------------- #
# capacity oracle
# --------------------------------------------------------------------------- #


def _oracle(
    clock: FakeClock, limits: QuotaLimits, obj: SlaObjective, tcfg: ThrottleConfig
) -> tuple[CapacityOracle, QuotaAccountant, ProviderThrottle, SlaTracker]:
    store = InMemoryGovernorStore()
    acct = QuotaAccountant("p", limits, store, clock=clock)
    thr = ProviderThrottle("p", tcfg, clock=clock)
    sla = SlaTracker("p", obj, clock=clock)
    return CapacityOracle("p", acct, thr, sla, clock=clock), acct, thr, sla


async def test_oracle_admits_when_healthy_and_unloaded() -> None:
    clock = FakeClock()
    oracle, *_ = _oracle(clock, QuotaLimits(concurrent_jobs=2), SlaObjective(), ThrottleConfig())
    verdict = await oracle.can_take(RenderCost(concurrent=1))
    assert verdict.admit is True
    assert verdict.reason is None
    assert verdict.seconds_until_free == 0.0


async def test_oracle_denies_on_quota() -> None:
    clock = FakeClock()
    oracle, acct, *_ = _oracle(
        clock, QuotaLimits(concurrent_jobs=1), SlaObjective(), ThrottleConfig()
    )
    await acct.reserve(RenderCost(concurrent=1))  # fill the one slot
    verdict = await oracle.can_take(RenderCost(concurrent=1))
    assert verdict.admit is False
    assert verdict.reason is DenyReason.QUOTA


async def test_oracle_denies_when_throttled_with_eta() -> None:
    clock = FakeClock()
    oracle, _, thr, _ = _oracle(clock, QuotaLimits(), SlaObjective(), ThrottleConfig())
    thr.note_rate_limited(retry_after_s=15.0)
    verdict = await oracle.can_take(RenderCost(concurrent=1))
    assert verdict.admit is False
    assert verdict.reason is DenyReason.THROTTLED
    assert verdict.seconds_until_free == pytest.approx(15.0, abs=1e-6)


async def test_oracle_unhealthy_dominates_other_reasons() -> None:
    clock = FakeClock()
    oracle, acct, thr, sla = _oracle(
        clock,
        QuotaLimits(concurrent_jobs=1),
        SlaObjective(min_samples=2, target_success_rate=1.0),
        ThrottleConfig(),
    )
    await acct.reserve(RenderCost(concurrent=1))  # quota full too
    thr.note_rate_limited(retry_after_s=5.0)  # throttled too
    sla.record_success(1.0)
    sla.record_failure(1.0)  # breach (zero-budget SLO)
    verdict = await oracle.can_take(RenderCost(concurrent=1))
    assert verdict.reason is DenyReason.UNHEALTHY
    assert verdict.grade is SlaGrade.F


async def test_oracle_unhealthy_alone_is_advisory_not_a_hard_block() -> None:
    # A breached provider with quota+pacing headroom is still admittable (so it can
    # recover via continued, deprioritised traffic) but flagged unhealthy.
    clock = FakeClock()
    oracle, _, _, sla = _oracle(
        clock,
        QuotaLimits(concurrent_jobs=4),
        SlaObjective(min_samples=2, target_success_rate=1.0),
        ThrottleConfig(rate_per_min=0.0),
    )
    sla.record_success(1.0)
    sla.record_failure(1.0)  # grade F
    verdict = await oracle.can_take(RenderCost(concurrent=1))
    assert verdict.admit is True  # not a hard block
    assert verdict.unhealthy is True
    assert verdict.reason is DenyReason.UNHEALTHY  # advisory signal still surfaced


def test_oracle_rank_prefers_admitted_healthy_over_unhealthy() -> None:
    from app.video.governor import CapacityVerdict

    healthy = CapacityVerdict("h", admit=True, reason=None, seconds_until_free=0.0,
                              grade=SlaGrade.A, quota_utilisation=0.5, throttle_wait_s=0.0)
    unhealthy = CapacityVerdict("u", admit=True, reason=DenyReason.UNHEALTHY,
                                seconds_until_free=0.0, grade=SlaGrade.F,
                                quota_utilisation=0.0, throttle_wait_s=0.0)
    blocked = CapacityVerdict("b", admit=False, reason=DenyReason.QUOTA,
                              seconds_until_free=0.0, grade=SlaGrade.A,
                              quota_utilisation=1.0, throttle_wait_s=0.0)
    top = best_provider([unhealthy, healthy, blocked])
    assert top is not None and top.provider == "h"
    # With no healthy option, an admittable-but-unhealthy provider beats a blocked one.
    fallback = best_provider([unhealthy, blocked])
    assert fallback is not None and fallback.provider == "u"


# --------------------------------------------------------------------------- #
# governor lease lifecycle + events
# --------------------------------------------------------------------------- #


def _governor(clock: FakeClock, profile: ProviderProfile,
              events: GovernorEventBus | None = None) -> ProviderGovernor:
    config = GovernorConfig(default_profile=profile)
    return ProviderGovernor(config, store=InMemoryGovernorStore(),
                            events=events or GovernorEventBus(), clock=clock)


async def test_governor_admit_complete_releases_concurrency() -> None:
    clock = FakeClock()
    gov = _governor(clock, ProviderProfile(quota=QuotaLimits(concurrent_jobs=1)))
    lease1 = await gov.admit("dashscope", RenderCost(video_seconds=5.0, concurrent=1))
    assert lease1.admitted is True
    # Second admit blocked while the first is in flight.
    lease2 = await gov.admit("dashscope", RenderCost(video_seconds=5.0, concurrent=1))
    assert lease2.admitted is False
    assert lease2.reason is DenyReason.QUOTA
    # Complete the first; a third now fits.
    await gov.complete(lease1, success=True, latency_ms=100.0)
    lease3 = await gov.admit("dashscope", RenderCost(video_seconds=5.0, concurrent=1))
    assert lease3.admitted is True


async def test_governor_rate_limited_completion_backs_off_and_emits() -> None:
    clock = FakeClock()
    events = GovernorEventBus()
    gov = _governor(
        clock,
        ProviderProfile(
            quota=QuotaLimits(concurrent_jobs=4),
            throttle=ThrottleConfig(rate_per_min=600.0, burst=5),
        ),
        events,
    )
    lease = await gov.admit("dashscope", RenderCost(video_seconds=5.0, concurrent=1))
    await gov.complete(lease, success=False, rate_limited=True, retry_after_s=20.0)
    # The provider is now parked; a fresh admit is denied as throttled.
    verdict = await gov.can_take("dashscope", RenderCost(concurrent=1))
    assert verdict.reason is DenyReason.THROTTLED
    assert verdict.seconds_until_free == pytest.approx(20.0, abs=1e-6)
    backoff_events = events.recent(code=EventCode.THROTTLE_BACKOFF)
    assert backoff_events and backoff_events[-1].limit == pytest.approx(20.0)


async def test_governor_lease_context_manager_completes_on_exit_and_error() -> None:
    clock = FakeClock()
    gov = _governor(clock, ProviderProfile(quota=QuotaLimits(concurrent_jobs=1)))

    async with gov.lease("p", RenderCost(concurrent=1)) as lease:
        assert lease.admitted is True
    # released on normal exit
    assert (await gov.can_take("p", RenderCost(concurrent=1))).admit is True

    with pytest.raises(ValueError):
        async with gov.lease("p", RenderCost(concurrent=1)) as lease:
            assert lease.admitted is True
            raise ValueError("boom")
    # released on error exit too
    assert (await gov.can_take("p", RenderCost(concurrent=1))).admit is True


async def test_governor_emits_sla_breach_then_recovery() -> None:
    clock = FakeClock()
    events = GovernorEventBus()
    gov = _governor(
        clock,
        ProviderProfile(
            quota=QuotaLimits(concurrent_jobs=10),
            sla=SlaObjective(min_samples=4, target_success_rate=0.9, window_size=10),
            throttle=ThrottleConfig(rate_per_min=0.0),  # unpaced: isolate the SLA path
        ),
        events,
    )
    # Drive 10 failures → breach.
    for _ in range(10):
        lease = await gov.admit("p", RenderCost(concurrent=1))
        await gov.complete(lease, success=False, latency_ms=10.0)
    assert events.recent(code=EventCode.SLA_BREACH, min_severity=Severity.CRITICAL)
    # Drive 10 successes → the bad samples age out → recovery.
    for _ in range(10):
        lease = await gov.admit("p", RenderCost(concurrent=1))
        await gov.complete(lease, success=True, latency_ms=10.0)
    assert events.recent(code=EventCode.SLA_RECOVERED)


async def test_governor_emits_quota_near_limit_once_per_crossing() -> None:
    clock = FakeClock()
    events = GovernorEventBus()
    gov = _governor(
        clock,
        ProviderProfile(
            quota=QuotaLimits(daily_video_seconds=100.0, concurrent_jobs=10,
                              alert_fractions=(0.75, 0.9)),
        ),
        events,
    )
    # Cross 75% then 90% in two admits.
    l1 = await gov.admit("p", RenderCost(video_seconds=80.0, concurrent=1))
    await gov.complete(l1, success=True, latency_ms=1.0)
    l2 = await gov.admit("p", RenderCost(video_seconds=15.0, concurrent=1))
    await gov.complete(l2, success=True, latency_ms=1.0)
    near = events.recent(code=EventCode.QUOTA_NEAR_LIMIT)
    fractions = {e.detail.get("fraction", e.limit) for e in near}  # noqa: F841
    # Two distinct crossings (0.75 then 0.9) ⇒ two alerts, not one per admit forever.
    assert len(near) == 2


async def test_governor_pick_provider_prefers_healthy_unloaded() -> None:
    clock = FakeClock()
    config = GovernorConfig(
        profiles={
            "healthy": ProviderProfile(quota=QuotaLimits(concurrent_jobs=4)),
            "breached": ProviderProfile(
                quota=QuotaLimits(concurrent_jobs=4),
                sla=SlaObjective(min_samples=2, target_success_rate=1.0),
            ),
        }
    )
    gov = ProviderGovernor(config, store=InMemoryGovernorStore(), clock=clock)
    # Breach the "breached" provider.
    lease = await gov.admit("breached", RenderCost(concurrent=1))
    await gov.complete(lease, success=True, latency_ms=1.0)
    lease = await gov.admit("breached", RenderCost(concurrent=1))
    await gov.complete(lease, success=False, latency_ms=1.0)

    pick = await gov.pick_provider(["breached", "healthy"], RenderCost(concurrent=1))
    assert pick is not None
    assert pick.provider == "healthy"


async def test_governor_fairshare_defers_to_starving_tenant() -> None:
    clock = FakeClock()
    # One concurrency slot only, so contention is real.
    config = GovernorConfig(
        default_profile=ProviderProfile(
            quota=QuotaLimits(concurrent_jobs=1),
            throttle=ThrottleConfig(rate_per_min=0.0),  # unpaced: isolate fair-share
        ),
        fairshare=FairShareConfig(starvation_age_s=10.0),
    )
    events = GovernorEventBus()
    gov = ProviderGovernor(config, store=InMemoryGovernorStore(), events=events, clock=clock)
    gov.register_tenant("whale", weight=100.0)
    gov.register_tenant("minnow", weight=1.0)

    # Whale grabs the only slot and holds it (no complete).
    whale_first = await gov.admit("p", RenderCost(concurrent=1), tenant_id="whale")
    assert whale_first.admitted is True
    # Minnow wants a slot but is denied (slot busy) — its demand stays sticky.
    minnow_denied = await gov.admit("p", RenderCost(concurrent=1), tenant_id="minnow")
    assert minnow_denied.admitted is False
    assert minnow_denied.reason is DenyReason.QUOTA

    # Time passes; the minnow ages into starvation. Even though the whale could be
    # served first by weight, the next whale admit must defer to the starving minnow.
    clock.advance(11.0)
    await gov.complete(whale_first, success=True, latency_ms=1.0)  # free the slot
    whale_again = await gov.admit("p", RenderCost(concurrent=1), tenant_id="whale")
    assert whale_again.admitted is False
    assert whale_again.reason is None  # a fair-share deferral, not a capacity denial
    starvation = events.recent(code=EventCode.FAIRSHARE_STARVATION)
    assert starvation and starvation[-1].scope == "minnow"

    # The minnow, retrying, now wins the freed slot.
    minnow_win = await gov.admit("p", RenderCost(concurrent=1), tenant_id="minnow")
    assert minnow_win.admitted is True


async def test_governor_complete_on_unadmitted_lease_is_noop() -> None:
    clock = FakeClock()
    gov = _governor(clock, ProviderProfile(quota=QuotaLimits(concurrent_jobs=1)))
    # Fill the only slot so the next admit is genuinely denied.
    held = await gov.admit("p", RenderCost(concurrent=1))
    assert held.admitted is True
    denied = await gov.admit("p", RenderCost(concurrent=1))
    assert denied.admitted is False
    # Completing the denied lease must not raise or drive the gauge negative.
    await gov.complete(denied, success=True, latency_ms=1.0)
    # Only the one admitted render recorded an SLA sample once completed.
    await gov.complete(held, success=True, latency_ms=1.0)
    assert gov.sla("p").samples == 1


# --------------------------------------------------------------------------- #
# default config sanity
# --------------------------------------------------------------------------- #


def test_default_video_profiles_carry_documented_limits() -> None:
    config = default_video_profiles()
    ds = config.profile_for("dashscope")
    assert ds.quota.daily_video_seconds == 1650.0  # §11.1 pool
    mm = config.profile_for("minimax")
    assert mm.quota.monthly_spend_usd == 30.0  # §11.1 USD cap
    # Unknown providers fall back to the lenient default profile (unbounded quota).
    unknown = config.profile_for("brand-new-provider")
    assert unknown.quota.daily_video_seconds is None


def test_event_bus_isolates_failing_sink() -> None:
    bus = GovernorEventBus(history=4)

    def bad_sink(_e: object) -> None:
        raise RuntimeError("sink down")

    bus.add_sink(bad_sink)
    from app.video.governor import GovernorEvent

    bus.emit(GovernorEvent(EventCode.SLA_BREACH, Severity.CRITICAL, "p", "msg", at=0.0))
    assert bus.sink_errors == 1
    assert bus.emitted == 1
    assert len(bus.recent()) == 1  # ring still recorded it
