"""Deterministic unit tests for the end-to-end render reliability coordinator.

No infra, no network, no real waiting: every collaborator is a *scripted fake*
and time is a :class:`ManualClock` advanced by a cooperative ``manual_sleep``.
The suite exercises the full contract — ranked attempts, failover across
providers, budget-abort, quality-escalation, deadline best-so-far, attempt-log
correctness, and graceful fallback — and asserts the coordinator **never**
silently returns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.video.reliability import (
    AttemptStatus,
    FallbackReason,
    ManualClock,
    ReliabilityConfig,
    ReliableRenderCoordinator,
    RenderResult,
    RenderTier,
    ShotSpec,
    build_candidates,
    make_manual_sleep,
)

# --------------------------------------------------------------------------- #
# Scripted fakes
# --------------------------------------------------------------------------- #


class FakeRouter:
    """A router whose per-provider behavior is fully scripted.

    ``behaviors[provider]`` is a list consumed one entry per ``render`` call:
      * an :class:`Exception` instance -> raised (provider/router failure);
      * a :class:`RenderResult` -> returned (a produced clip);
      * a 0-arg callable -> called (e.g. to advance the clock, then returns/raises).
    """

    def __init__(self, provider_order: list[str], behaviors: dict[str, list[object]]) -> None:
        self._order = provider_order
        self._behaviors = {k: list(v) for k, v in behaviors.items()}
        self.calls: list[str] = []

    def candidates(self, shot: ShotSpec) -> list[str]:
        return list(self._order)

    async def render(self, provider: str, shot: ShotSpec) -> RenderResult:
        self.calls.append(provider)
        queue = self._behaviors.get(provider, [])
        if not queue:
            raise AssertionError(f"unexpected extra render call for {provider!r}")
        behavior = queue.pop(0)
        if callable(behavior) and not isinstance(behavior, RenderResult):
            behavior = behavior()
        if isinstance(behavior, BaseException):
            raise behavior
        assert isinstance(behavior, RenderResult)
        return behavior


class FakeGovernor:
    """Admission + load per provider; defaults to admit-all, zero load."""

    def __init__(
        self,
        *,
        admit: dict[str, bool] | None = None,
        load: dict[str, float] | None = None,
    ) -> None:
        self._admit = admit or {}
        self._load = load or {}

    def admit(self, provider: str, shot: ShotSpec) -> bool:
        return self._admit.get(provider, True)

    def load_factor(self, provider: str) -> float:
        return self._load.get(provider, 0.0)


class FakeReputation:
    def __init__(self, rep: dict[str, float] | None = None) -> None:
        self._rep = rep or {}

    def reputation(self, provider: str) -> float:
        return self._rep.get(provider, 0.5)


class FakeGate:
    """Returns a scripted score; can also key off the produced result's provider."""

    def __init__(self, scores: dict[str, float], *, default: float = 1.0) -> None:
        self._scores = scores
        self._default = default
        self.calls: list[str] = []

    async def score(self, shot: ShotSpec, result: RenderResult) -> float:
        self.calls.append(result.provider)
        return self._scores.get(result.provider, self._default)


@dataclass
class FakeReservation:
    amount: float
    ledger: FakeBudget
    _closed: bool = False

    @property
    def amount_usd(self) -> float:
        return self.amount

    def settle(self, actual_usd: float) -> None:
        assert not self._closed, "reservation double-closed"
        self._closed = True
        self.ledger.charged += actual_usd
        # release the held remainder back
        self.ledger.held -= self.amount

    def release(self) -> None:
        assert not self._closed, "reservation double-closed"
        self._closed = True
        self.ledger.held -= self.amount
        self.ledger.releases += 1


@dataclass
class FakeBudget:
    """A simple USD budget ledger with reserve/settle/release accounting."""

    total: float = 100.0
    per_provider_cost: dict[str, float] = field(default_factory=dict)
    default_cost: float = 1.0
    held: float = 0.0
    charged: float = 0.0
    releases: int = 0
    deny_after_charged: float | None = None

    def estimate(self, provider: str, shot: ShotSpec) -> float:
        return self.per_provider_cost.get(provider, self.default_cost)

    def remaining_usd(self) -> float:
        return self.total - self.charged - self.held

    def reserve(self, provider: str, amount_usd: float) -> FakeReservation | None:
        if self.deny_after_charged is not None and self.charged >= self.deny_after_charged:
            return None
        if amount_usd > self.remaining_usd():
            return None
        self.held += amount_usd
        return FakeReservation(amount=amount_usd, ledger=self)


class RecordingJobs:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def started(self, shot: ShotSpec) -> None:
        self.events.append(("started", shot.shot_id))

    def progress(self, shot: ShotSpec, event: str, fields: object) -> None:
        self.events.append(("progress", event))

    def finished(self, shot: ShotSpec, outcome_ok: bool, tier: int) -> None:
        self.events.append(("finished", (outcome_ok, tier)))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def good_clip(
    provider: str, *, tier: RenderTier = RenderTier.FULL_VIDEO, cost: float = 1.0
) -> RenderResult:
    return RenderResult(
        shot_id="s1",
        provider=provider,
        tier=tier,
        uri=f"oss://{provider}/clip.mp4",
        quality=0.0,  # the gate sets the real score
        cost_usd=cost,
        video_seconds=5.0,
    )


def make_coordinator(
    *,
    router: FakeRouter,
    governor: FakeGovernor | None = None,
    reputation: FakeReputation | None = None,
    gate: FakeGate | None = None,
    budget: FakeBudget | None = None,
    config: ReliabilityConfig | None = None,
    jobs: RecordingJobs | None = None,
    clock: ManualClock | None = None,
) -> ReliableRenderCoordinator:
    clk = clock or ManualClock()
    return ReliableRenderCoordinator(
        router=router,
        governor=governor or FakeGovernor(),
        reputation=reputation or FakeReputation(),
        quality_gate=gate or FakeGate({}, default=1.0),
        budget=budget or FakeBudget(),
        config=config or ReliabilityConfig(),
        jobs=jobs,
        clock=clk,
        sleep=make_manual_sleep(clk),
    )


SHOT = ShotSpec(shot_id="s1", est_video_seconds=5.0, deadline_s=30.0, min_quality=0.6)


# --------------------------------------------------------------------------- #
# 1. Ranking
# --------------------------------------------------------------------------- #


def test_candidate_ranking_orders_by_reputation_load_and_cost() -> None:
    router = FakeRouter(["c", "a", "b"], {})
    plan = build_candidates(
        SHOT,
        router=router,
        governor=FakeGovernor(load={"a": 0.0, "b": 0.9, "c": 0.0}),
        reputation=FakeReputation({"a": 0.9, "b": 0.9, "c": 0.4}),
        budget=FakeBudget(per_provider_cost={"a": 1.0, "b": 1.0, "c": 1.0}),
        config=ReliabilityConfig(),
    )
    order = [c.provider for c in plan.ranked]
    # a: high rep + idle -> best. b: high rep but loaded. c: low rep.
    assert order[0] == "a"
    assert set(order) == {"a", "b", "c"}
    assert plan.pruned == []


async def test_ranked_attempts_try_best_provider_first_and_accept() -> None:
    router = FakeRouter(
        ["lo", "hi"],
        {"hi": [good_clip("hi")], "lo": [good_clip("lo")]},
    )
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"hi": 0.95, "lo": 0.2}),
        gate=FakeGate({}, default=0.9),
    )
    out = await coord.render(SHOT)
    assert out.ok is True
    assert out.result.provider == "hi"  # highest reputation tried first
    assert router.calls == ["hi"]  # never needed the worse provider
    assert out.fallback_reason is FallbackReason.NONE
    assert out.log.final_status is AttemptStatus.ACCEPTED


# --------------------------------------------------------------------------- #
# 2. Failover across providers
# --------------------------------------------------------------------------- #


async def test_failover_when_first_provider_errors() -> None:
    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [RuntimeError("provider exploded")], "p2": [good_clip("p2")]},
    )
    budget = FakeBudget()
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.8}),
        gate=FakeGate({}, default=0.9),
        budget=budget,
    )
    out = await coord.render(SHOT)
    assert out.ok is True
    assert out.result.provider == "p2"
    assert router.calls == ["p1", "p2"]
    # The failed attempt released its reservation -> charged only for p2.
    assert budget.releases == 1
    assert pytest.approx(budget.charged) == 1.0
    statuses = [r.status for r in out.log.attempts]
    assert statuses == [AttemptStatus.PROVIDER_ERROR, AttemptStatus.ACCEPTED]
    assert "RuntimeError" in out.log.attempts[0].detail


async def test_coordinator_level_retry_then_failover() -> None:
    # per_provider_attempts=2: p1 errors twice (retried once) then we move on.
    router = FakeRouter(
        ["p1", "p2"],
        {
            "p1": [RuntimeError("boom1"), RuntimeError("boom2")],
            "p2": [good_clip("p2")],
        },
    )
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=FakeGate({}, default=0.9),
        config=ReliabilityConfig(per_provider_attempts=2, retry_backoff_base_s=0.1),
    )
    out = await coord.render(SHOT)
    assert out.result.provider == "p2"
    assert router.calls == ["p1", "p1", "p2"]  # retried p1 once, then escalated


# --------------------------------------------------------------------------- #
# 3. Budget abort
# --------------------------------------------------------------------------- #


async def test_budget_abort_when_reservation_denied() -> None:
    # Budget can't afford anything -> reserve() returns None -> clean abort.
    router = FakeRouter(["p1", "p2"], {})
    budget = FakeBudget(total=0.0, default_cost=5.0)
    coord = make_coordinator(
        router=router,
        budget=budget,
        gate=FakeGate({}, default=0.9),
    )
    out = await coord.render(SHOT)
    assert out.ok is True  # never returns nothing
    assert out.result.degraded is True
    assert out.result.tier is RenderTier.NARRATED_TEXT
    assert out.fallback_reason is FallbackReason.NO_CANDIDATES  # pruned at pre-flight
    assert router.calls == []  # never even called the router


async def test_budget_preflight_prunes_unaffordable_providers() -> None:
    # The per-provider estimate (1.0) exceeds the remaining budget (0.5), so both
    # providers are pruned at the pre-flight stage and never rendered.
    router = FakeRouter(["p1", "p2"], {})
    budget = FakeBudget(total=0.5, default_cost=1.0)
    coord = make_coordinator(router=router, budget=budget, gate=FakeGate({}, default=0.9))
    out = await coord.render(SHOT)
    assert out.result.degraded is True
    assert router.calls == []
    assert all(
        r.status is AttemptStatus.SKIPPED_NO_BUDGET_HEADROOM for r in out.log.attempts
    )
    assert out.fallback_reason is FallbackReason.NO_CANDIDATES


async def test_budget_drains_mid_run_aborts_remaining_providers() -> None:
    # p1 is rejected by the gate (releasing its hold but the ledger now models a
    # spent budget); p2's reservation is then denied -> clean budget abort.
    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [good_clip("p1")], "p2": [good_clip("p2")]},
    )

    class DrainingBudget(FakeBudget):
        def __init__(self) -> None:
            super().__init__(total=100.0, default_cost=1.0)
            self._calls = 0

        def reserve(self, provider: str, amount_usd: float) -> FakeReservation | None:
            self._calls += 1
            if self._calls == 1:
                return super().reserve(provider, amount_usd)
            return None  # budget drained before the second provider

    budget = DrainingBudget()
    gate = FakeGate({"p1": 0.2, "p2": 0.9})  # p1 rejected -> escalate to p2
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=gate,
        budget=budget,
    )
    out = await coord.render(SHOT)
    assert out.result.degraded is True
    assert router.calls == ["p1"]  # p2 never rendered: reservation denied first
    statuses = [r.status for r in out.log.attempts]
    assert statuses == [AttemptStatus.QUALITY_REJECTED, AttemptStatus.BUDGET_DENIED]
    assert out.fallback_reason is FallbackReason.BUDGET_EXHAUSTED
    # The shipped best-so-far is p1's rejected clip, flagged degraded.
    assert out.result.provider == "p1"


async def test_budget_denied_reservation_records_budget_abort() -> None:
    # estimate fits remaining_usd (pre-flight passes) but reserve() refuses.
    class StingyBudget(FakeBudget):
        def reserve(self, provider: str, amount_usd: float) -> FakeReservation | None:
            return None  # always deny the actual hold

    router = FakeRouter(["p1", "p2"], {})
    budget = StingyBudget(total=100.0, default_cost=1.0)
    coord = make_coordinator(router=router, budget=budget, gate=FakeGate({}, default=0.9))
    out = await coord.render(SHOT)
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.BUDGET_EXHAUSTED
    assert out.log.attempts[0].status is AttemptStatus.BUDGET_DENIED
    assert router.calls == []  # aborted before rendering


# --------------------------------------------------------------------------- #
# 4. Quality escalation
# --------------------------------------------------------------------------- #


async def test_quality_rejection_escalates_to_next_provider() -> None:
    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [good_clip("p1")], "p2": [good_clip("p2")]},
    )
    budget = FakeBudget()
    # p1 produces a clip scored 0.3 (< 0.6 floor) -> reject + escalate; p2 scores 0.8.
    gate = FakeGate({"p1": 0.3, "p2": 0.8})
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=gate,
        budget=budget,
    )
    out = await coord.render(SHOT)
    assert out.result.provider == "p2"
    assert out.result.quality == pytest.approx(0.8)
    assert router.calls == ["p1", "p2"]
    assert gate.calls == ["p1", "p2"]
    statuses = [r.status for r in out.log.attempts]
    assert statuses == [AttemptStatus.QUALITY_REJECTED, AttemptStatus.ACCEPTED]
    # The rejected clip never charged the budget.
    assert budget.releases == 1
    assert pytest.approx(budget.charged) == 1.0


async def test_all_clips_below_floor_falls_back_to_best_so_far() -> None:
    # Same tier so the higher *quality* rejected clip wins best-so-far.
    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [good_clip("p1")], "p2": [good_clip("p2")]},
    )
    # Neither clears 0.6; p2 scored higher so it becomes best-so-far.
    gate = FakeGate({"p1": 0.4, "p2": 0.5})
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=gate,
    )
    out = await coord.render(SHOT)
    assert out.ok is True
    assert out.result.provider == "p2"  # best-so-far by (tier, quality)
    assert out.result.quality == pytest.approx(0.5)
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.QUALITY_FLOOR


async def test_best_so_far_prefers_higher_tier_over_higher_quality() -> None:
    # p1 is full-video at q=0.4, p2 is a lower-tier animatic at q=0.55; both below
    # the 0.6 floor. The higher *tier* p1 wins best-so-far even though p2 scored
    # higher — fidelity tier dominates the §12.4 ladder.
    router = FakeRouter(
        ["p1", "p2"],
        {
            "p1": [good_clip("p1", tier=RenderTier.FULL_VIDEO)],
            "p2": [good_clip("p2", tier=RenderTier.ANIMATIC)],
        },
    )
    gate = FakeGate({"p1": 0.4, "p2": 0.55})
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=gate,
    )
    out = await coord.render(SHOT)
    assert out.result.provider == "p1"
    assert out.result.tier is RenderTier.FULL_VIDEO
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.QUALITY_FLOOR


# --------------------------------------------------------------------------- #
# 5. Deadline best-so-far
# --------------------------------------------------------------------------- #


async def test_deadline_returns_best_so_far_before_reader_arrives() -> None:
    clock = ManualClock()

    # Rendering p1 itself consumes the whole deadline (it produces a rejectable
    # clip but slowly); the deadline check before p2 then fires, so p2's render is
    # never called and we ship p1 as best-so-far.
    def slow_p1() -> RenderResult:
        clock.advance(100.0)  # push past the 10s deadline
        return good_clip("p1")

    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [slow_p1], "p2": [good_clip("p2")]},
    )
    gate = FakeGate({"p1": 0.4, "p2": 0.99})
    shot = ShotSpec(shot_id="s1", deadline_s=10.0, min_quality=0.6)
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.5}),
        gate=gate,
        clock=clock,
    )
    out = await coord.render(shot)
    # p1 rejected (kept as best-so-far). Before trying p2 the deadline check fires
    # -> we never call p2's render at all.
    assert router.calls == ["p1"]
    assert out.result.provider == "p1"  # best-so-far returned
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.DEADLINE_EXCEEDED
    assert any(r.status is AttemptStatus.DEADLINE_EXCEEDED for r in out.log.attempts)


async def test_deadline_with_no_result_yet_returns_degraded_card() -> None:
    clock = ManualClock()
    shot = ShotSpec(shot_id="s1", deadline_s=5.0, min_quality=0.6)

    def burn_then_fail() -> RenderResult:
        clock.advance(10.0)
        raise RuntimeError("too slow")

    router = FakeRouter(["p1", "p2"], {"p1": [burn_then_fail]})
    coord = make_coordinator(router=router, gate=FakeGate({}, default=0.9), clock=clock)
    out = await coord.render(shot)
    assert out.ok is True
    assert out.result.degraded is True
    assert out.result.tier is RenderTier.NARRATED_TEXT
    assert out.fallback_reason is FallbackReason.DEADLINE_EXCEEDED


# --------------------------------------------------------------------------- #
# 6. Attempt-log correctness
# --------------------------------------------------------------------------- #


async def test_attempt_log_records_every_provider_and_reason() -> None:
    router = FakeRouter(
        ["good", "err", "lowq", "blocked"],
        {
            "err": [RuntimeError("nope")],
            "lowq": [good_clip("lowq")],
            "good": [good_clip("good")],
        },
    )
    governor = FakeGovernor(admit={"blocked": False})
    reputation = FakeReputation({"err": 0.99, "lowq": 0.8, "good": 0.7, "blocked": 0.95})
    gate = FakeGate({"lowq": 0.2, "good": 0.9})
    budget = FakeBudget()
    coord = make_coordinator(
        router=router,
        governor=governor,
        reputation=reputation,
        gate=gate,
        budget=budget,
    )
    out = await coord.render(SHOT)
    assert out.result.provider == "good"

    by_provider = {r.provider: r for r in out.log.attempts}
    # blocked provider pruned by the governor (recorded, never rendered).
    assert by_provider["blocked"].status is AttemptStatus.GOVERNOR_BLOCKED
    assert by_provider["err"].status is AttemptStatus.PROVIDER_ERROR
    assert by_provider["lowq"].status is AttemptStatus.QUALITY_REJECTED
    assert by_provider["good"].status is AttemptStatus.ACCEPTED
    assert "blocked" not in router.calls  # truly never tried

    # Ranking recorded; totals stamped; tier/status final.
    assert "blocked" not in out.log.ranked_providers
    assert out.log.final_status is AttemptStatus.ACCEPTED
    assert out.log.final_tier is RenderTier.FULL_VIDEO
    assert out.log.total_elapsed_s >= 0.0
    assert out.log.total_cost_charged_usd == pytest.approx(1.0)
    # only the winner charged; the rejected/errored attempts released.
    assert budget.releases == 2


async def test_attempt_log_timestamps_are_relative_and_monotonic() -> None:
    clock = ManualClock()

    def tick_clip() -> RenderResult:
        clock.advance(2.0)
        return good_clip("p1")

    router = FakeRouter(["p1"], {"p1": [tick_clip]})
    coord = make_coordinator(router=router, gate=FakeGate({}, default=0.9), clock=clock)
    out = await coord.render(SHOT)
    rec = out.log.attempts[0]
    assert rec.started_at_s == pytest.approx(0.0)
    assert rec.ended_at_s == pytest.approx(2.0)
    assert rec.elapsed_s == pytest.approx(2.0)
    assert out.log.deadline_s == pytest.approx(SHOT.deadline_s)


# --------------------------------------------------------------------------- #
# 7. Graceful fallback (never nothing)
# --------------------------------------------------------------------------- #


async def test_graceful_fallback_when_all_providers_fail() -> None:
    router = FakeRouter(
        ["p1", "p2"],
        {"p1": [RuntimeError("a")], "p2": [RuntimeError("b")]},
    )
    budget = FakeBudget()
    coord = make_coordinator(router=router, budget=budget, gate=FakeGate({}, default=0.9))
    out = await coord.render(SHOT)
    assert out.ok is True
    assert out.result.degraded is True
    assert out.result.tier is RenderTier.NARRATED_TEXT
    assert out.result.cost_usd == 0.0
    assert out.fallback_reason is FallbackReason.ALL_PROVIDERS_FAILED
    assert budget.releases == 2  # both failed attempts released their holds


async def test_no_candidates_at_all_still_ships_a_card() -> None:
    router = FakeRouter([], {})  # router knows no providers
    coord = make_coordinator(router=router)
    out = await coord.render(SHOT)
    assert out.ok is True
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.NO_CANDIDATES
    assert out.log.attempts == []


async def test_gate_that_raises_is_treated_as_rejection_not_crash() -> None:
    class ExplodingGate:
        async def score(self, shot: ShotSpec, result: RenderResult) -> float:
            raise ValueError("gate model down")

    router = FakeRouter(["p1"], {"p1": [good_clip("p1")]})
    budget = FakeBudget()
    coord = make_coordinator(router=router, budget=budget)
    coord._gate = ExplodingGate()
    out = await coord.render(SHOT)
    # The gate error rejects the clip; with no alternative we ship a degraded card.
    assert out.ok is True
    assert out.result.degraded is True
    assert out.log.attempts[0].status is AttemptStatus.QUALITY_REJECTED
    assert budget.releases == 1  # the un-gradeable attempt did not charge


async def test_job_sink_lifecycle_events_emitted() -> None:
    router = FakeRouter(["p1"], {"p1": [good_clip("p1")]})
    jobs = RecordingJobs()
    coord = make_coordinator(router=router, gate=FakeGate({}, default=0.9), jobs=jobs)
    out = await coord.render(SHOT)
    assert ("started", "s1") in jobs.events
    assert any(e[0] == "finished" for e in jobs.events)
    finished = next(e for e in jobs.events if e[0] == "finished")
    assert finished[1] == (True, int(out.result.tier))


# --------------------------------------------------------------------------- #
# 8. Config + invariants
# --------------------------------------------------------------------------- #


def test_config_from_settings_is_tolerant_of_missing_attrs() -> None:
    class BareSettings:
        pass

    cfg = ReliabilityConfig.from_settings(BareSettings())
    assert cfg.max_providers == ReliabilityConfig().max_providers


def test_config_from_settings_reads_present_attrs() -> None:
    class S:
        video_reliability_max_providers = 2
        video_reliability_min_quality = 0.8

    cfg = ReliabilityConfig.from_settings(S())
    assert cfg.max_providers == 2
    assert cfg.default_min_quality == 0.8


async def test_max_providers_caps_attempts() -> None:
    router = FakeRouter(
        ["p1", "p2", "p3"],
        {"p1": [RuntimeError("x")], "p2": [RuntimeError("y")], "p3": [good_clip("p3")]},
    )
    coord = make_coordinator(
        router=router,
        reputation=FakeReputation({"p1": 0.9, "p2": 0.8, "p3": 0.7}),
        gate=FakeGate({}, default=0.9),
        config=ReliabilityConfig(max_providers=2),
    )
    out = await coord.render(SHOT)
    # Only the top-2 ranked candidates are tried; p3 is never reached.
    assert router.calls == ["p1", "p2"]
    assert out.result.degraded is True
    assert out.fallback_reason is FallbackReason.ALL_PROVIDERS_FAILED
