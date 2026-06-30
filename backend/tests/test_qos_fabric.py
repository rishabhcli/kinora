"""QoS backpressure & priority fabric — deterministic policy tests (kinora.md §4.9/§12.2).

Every case runs against a :class:`app.qos.clock.VirtualClock` + the seeded
:class:`app.qos.load.LoadGen`, so there is no infra, no network, and no sleeping.
Covers: strict priority ordering, WFQ shares + no-starvation, admission /
backpressure / deferral under saturation, aging promotion, deadline (EDF) urgency,
least-value-first load shedding, and per-book fairness.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderPriority
from app.qos import (
    AdmissionPolicy,
    LoadGen,
    QoSClass,
    QoSConfig,
    QoSItem,
    QoSScheduler,
    SheddingReason,
    VirtualClock,
    aging_boost,
    allocate_slots,
    edf_key,
    fair_book_allocation,
    fair_share_fractions,
    is_expired,
    is_urgent,
    max_min_fair_shares,
    select_victims,
    starvation_free,
    urgency_score,
)
from app.qos.adapter import job_to_qos_item
from app.qos.clock import WallClock

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=1000.0)


@pytest.fixture
def gen(clock: VirtualClock) -> LoadGen:
    return LoadGen(clock, seed=7)


def _make(
    clock: VirtualClock,
    qos_class: QoSClass,
    *,
    id: str,
    book_id: str = "book_a",
    session_id: str | None = None,
    eta_s: float | None = None,
    value: float = 1.0,
    cost_s: float = 5.0,
    enqueued_at: float | None = None,
) -> QoSItem:
    now = clock.now() if enqueued_at is None else enqueued_at
    return QoSItem(
        id=id,
        qos_class=qos_class,
        book_id=book_id,
        session_id=session_id,
        enqueued_at=now,
        deadline=None if eta_s is None else now + eta_s,
        eta_s=eta_s,
        value=value,
        cost_s=cost_s,
    )


# --------------------------------------------------------------------------- #
# clock
# --------------------------------------------------------------------------- #


def test_virtual_clock_advances_and_never_rewinds() -> None:
    c = VirtualClock(start=5.0)
    assert c.now() == 5.0
    assert c.advance(2.5) == 7.5
    assert c.set(10.0) == 10.0
    with pytest.raises(ValueError):
        c.advance(-1.0)
    with pytest.raises(ValueError):
        c.set(9.0)


def test_wall_clock_monotonic_nondecreasing() -> None:
    c = WallClock()
    a = c.now()
    b = c.now()
    assert b >= a


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


def test_qos_class_priority_and_traits() -> None:
    assert QoSClass.COMMITTED < QoSClass.SPECULATIVE < QoSClass.COLD
    assert not QoSClass.COMMITTED.droppable and not QoSClass.COMMITTED.preemptible
    assert QoSClass.SPECULATIVE.droppable and QoSClass.COLD.droppable


def test_class_priority_round_trip() -> None:
    assert QoSClass.from_priority("committed") is QoSClass.COMMITTED
    assert QoSClass.from_priority("speculative") is QoSClass.SPECULATIVE
    assert QoSClass.from_priority("keyframe") is QoSClass.COLD
    assert QoSClass.COLD.to_priority_value() == "keyframe"


def test_item_slack_value_density_fairness_key(clock: VirtualClock) -> None:
    it = _make(clock, QoSClass.SPECULATIVE, id="x", eta_s=10.0, value=8.0, cost_s=4.0)
    assert it.slack_s(clock.now()) == pytest.approx(10.0)
    assert it.value_density() == pytest.approx(2.0)
    assert it.fairness_key == "book_a"
    it.tenant_id = "tenant_1"
    assert it.fairness_key == "tenant_1"


# --------------------------------------------------------------------------- #
# strict priority + WFQ allocation
# --------------------------------------------------------------------------- #


def test_committed_reservation_protected_under_flood() -> None:
    cfg = QoSConfig(total_slots=6, committed_reserved_slots=4)
    alloc = allocate_slots(
        available_slots=6,
        backlog={QoSClass.COMMITTED: 10, QoSClass.SPECULATIVE: 50, QoSClass.COLD: 50},
        config=cfg,
    )
    # Committed gets its reserved 4 first; the WFQ tier then deals the last 2.
    assert alloc.get(QoSClass.COMMITTED) >= 4
    assert alloc.total == 6


def test_wfq_gives_cold_a_share_never_starves() -> None:
    cfg = QoSConfig(
        total_slots=8,
        committed_reserved_slots=0,
        wfq_weights={QoSClass.COMMITTED: 8.0, QoSClass.SPECULATIVE: 2.0, QoSClass.COLD: 1.0},
    )
    alloc = allocate_slots(
        available_slots=8,
        backlog={QoSClass.COMMITTED: 100, QoSClass.SPECULATIVE: 100, QoSClass.COLD: 100},
        config=cfg,
    )
    # Even with committed flooded, cold gets at least one slot — no full starvation.
    assert alloc.get(QoSClass.COLD) >= 1
    assert alloc.get(QoSClass.SPECULATIVE) >= 1
    assert alloc.get(QoSClass.COMMITTED) >= alloc.get(QoSClass.SPECULATIVE) >= alloc.get(
        QoSClass.COLD
    )
    assert alloc.total == 8


def test_wfq_is_work_conserving_no_slot_to_empty_class() -> None:
    cfg = QoSConfig(total_slots=6, committed_reserved_slots=4)
    alloc = allocate_slots(
        available_slots=6,
        backlog={QoSClass.COMMITTED: 1, QoSClass.SPECULATIVE: 0, QoSClass.COLD: 10},
        config=cfg,
    )
    # Speculative is empty -> 0; its slots flow to cold (work-conserving).
    assert alloc.get(QoSClass.SPECULATIVE) == 0
    assert alloc.get(QoSClass.COMMITTED) == 1
    assert alloc.get(QoSClass.COLD) == 5
    assert alloc.total == 6


def test_wfq_share_fractions_sum_to_one() -> None:
    cfg = QoSConfig()
    fracs = fair_share_fractions(cfg)
    assert sum(fracs.values()) == pytest.approx(1.0)
    assert fracs[QoSClass.COMMITTED] > fracs[QoSClass.SPECULATIVE] > fracs[QoSClass.COLD]


def test_long_run_wfq_shares_track_weights() -> None:
    # Run many dispatch rounds against an always-full backlog of all classes; with
    # ample slots per round (so the anti-starvation floor is a small fraction) the
    # cumulative service tracks the WFQ weights — committed dominates, cold non-trivial.
    cfg = QoSConfig(
        total_slots=20,
        committed_reserved_slots=0,
        wfq_weights={QoSClass.COMMITTED: 4.0, QoSClass.SPECULATIVE: 2.0, QoSClass.COLD: 1.0},
    )
    served = dict.fromkeys(QoSClass, 0)
    for _ in range(200):
        alloc = allocate_slots(
            available_slots=20,
            backlog={QoSClass.COMMITTED: 99, QoSClass.SPECULATIVE: 99, QoSClass.COLD: 99},
            config=cfg,
        )
        for c in QoSClass:
            served[c] += alloc.get(c)
    total = sum(served.values())
    # Committed > speculative > cold, and cold never starved across 200 rounds.
    assert served[QoSClass.COMMITTED] / total > served[QoSClass.SPECULATIVE] / total
    assert served[QoSClass.SPECULATIVE] / total > served[QoSClass.COLD] / total
    assert served[QoSClass.COLD] > 0
    # Committed gets roughly its 4/7 weight share (loose bound for the floor + rounding).
    assert served[QoSClass.COMMITTED] / total > 0.45


def test_scheduler_strict_priority_serves_committed_first(
    clock: VirtualClock, gen: LoadGen
) -> None:
    s = QoSScheduler(config=QoSConfig(total_slots=4, committed_reserved_slots=4), clock=clock)
    for _ in range(2):
        s.admit(gen.item(qos_class=QoSClass.COMMITTED, eta_s=10.0))
    for _ in range(5):
        s.admit(gen.item(qos_class=QoSClass.SPECULATIVE, eta_s=120.0))
    result = s.dispatch(available_slots=4)
    classes = [it.qos_class for it in result.dispatched]
    assert classes[:2] == [QoSClass.COMMITTED, QoSClass.COMMITTED]


# --------------------------------------------------------------------------- #
# admission control + backpressure
# --------------------------------------------------------------------------- #


def test_committed_always_admitted_even_when_saturated(clock: VirtualClock) -> None:
    cfg = QoSConfig(backpressure_depth=10)
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.COMMITTED, id="c", eta_s=5.0)
    v = pol.check(it, clock.now(), total_depth=999)
    assert v.admit and v.reason is SheddingReason.ADMIT


def test_speculative_rejected_at_hard_backpressure(clock: VirtualClock) -> None:
    cfg = QoSConfig(backpressure_depth=10, speculation_slowdown_depth=8)
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.SPECULATIVE, id="s", eta_s=60.0)
    v = pol.check(it, clock.now(), total_depth=10)
    assert not v.admit and not v.defer and v.reason is SheddingReason.SHED_BACKPRESSURE


def test_speculative_deferred_in_slowdown_band(clock: VirtualClock) -> None:
    cfg = QoSConfig(backpressure_depth=10, speculation_slowdown_depth=8)
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.SPECULATIVE, id="s", eta_s=60.0)
    v = pol.check(it, clock.now(), total_depth=8)
    assert not v.admit and v.defer and v.reason is SheddingReason.DEFER_SATURATED
    assert pol.should_slow_speculation(8)
    assert not pol.should_slow_speculation(7)


def test_admission_session_cap(clock: VirtualClock) -> None:
    cfg = QoSConfig(session_cap=2, backpressure_depth=100, speculation_slowdown_depth=99)
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.SPECULATIVE, id="s", session_id="sess", eta_s=60.0)
    v = pol.check(it, clock.now(), total_depth=1, session_inflight=2)
    assert not v.admit and v.reason is SheddingReason.SHED_TENANT_OVER_FAIR_SHARE


def test_admission_per_book_speculative_cap(clock: VirtualClock) -> None:
    cfg = QoSConfig(
        per_book_speculative_cap=3, backpressure_depth=100, speculation_slowdown_depth=99
    )
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.SPECULATIVE, id="s", eta_s=60.0)
    v = pol.check(it, clock.now(), total_depth=1, book_speculative_depth=3)
    assert not v.admit and v.reason is SheddingReason.SHED_TENANT_OVER_FAIR_SHARE


def test_admission_rejects_expired_deadline(clock: VirtualClock) -> None:
    cfg = QoSConfig(deadline_expiry_grace_s=1.0)
    pol = AdmissionPolicy(cfg)
    it = _make(clock, QoSClass.SPECULATIVE, id="s", eta_s=-5.0)  # already past
    v = pol.check(it, clock.now(), total_depth=0)
    assert not v.admit and v.reason is SheddingReason.SHED_OVER_DEADLINE


def test_scheduler_admit_adds_or_rejects(clock: VirtualClock, gen: LoadGen) -> None:
    cfg = QoSConfig(backpressure_depth=3, speculation_slowdown_depth=2)
    s = QoSScheduler(config=cfg, clock=clock)
    # Fill with committed (always admitted) past the threshold.
    for _ in range(5):
        assert s.admit(gen.item(qos_class=QoSClass.COMMITTED, eta_s=5.0)).admit
    # A new speculative is now shed by backpressure.
    v = s.admit(gen.item(qos_class=QoSClass.SPECULATIVE, eta_s=60.0))
    assert not v.admit
    assert s.depth == 5  # the speculative was not enqueued


# --------------------------------------------------------------------------- #
# aging / anti-starvation
# --------------------------------------------------------------------------- #


def test_aging_promotes_only_after_step_and_is_bounded(clock: VirtualClock) -> None:
    cfg = QoSConfig(aging_step_s=20.0, aging_max_boost=1)
    it = _make(clock, QoSClass.COLD, id="cold", enqueued_at=clock.now())
    assert aging_boost(it, clock.now(), config=cfg) == 0
    clock.advance(19.0)
    assert aging_boost(it, clock.now(), config=cfg) == 0
    clock.advance(2.0)  # 21s -> one step
    assert aging_boost(it, clock.now(), config=cfg) == 1
    clock.advance(1000.0)  # far past -> still capped at max_boost
    assert aging_boost(it, clock.now(), config=cfg) == 1


def test_committed_never_ages(clock: VirtualClock) -> None:
    cfg = QoSConfig(aging_step_s=1.0, aging_max_boost=5)
    it = _make(clock, QoSClass.COMMITTED, id="c", enqueued_at=clock.now())
    clock.advance(100.0)
    assert aging_boost(it, clock.now(), config=cfg) == 0


def test_aged_cold_competes_with_speculative_in_dispatch(clock: VirtualClock) -> None:
    # An old cold item, after aging, should be served alongside speculative work
    # in a round where only one non-committed slot is available.
    cfg = QoSConfig(
        total_slots=1,
        committed_reserved_slots=0,
        aging_step_s=10.0,
        aging_max_boost=1,
        wfq_weights={QoSClass.COMMITTED: 8.0, QoSClass.SPECULATIVE: 2.0, QoSClass.COLD: 1.0},
    )
    s = QoSScheduler(config=cfg, clock=clock)
    old_cold = _make(clock, QoSClass.COLD, id="old_cold", enqueued_at=clock.now())
    s.admit(old_cold)
    clock.advance(15.0)  # cold ages one step -> effective speculative
    fresh_spec = _make(clock, QoSClass.SPECULATIVE, id="fresh_spec", eta_s=200.0)
    s.admit(fresh_spec)
    result = s.dispatch(available_slots=1)
    # The aged cold item is now in the speculative effective-class pool and, being
    # the more urgent / older arrival, can be dispatched — proving anti-starvation.
    assert len(result.dispatched) == 1


def test_starvation_flag_surfaces(clock: VirtualClock) -> None:
    cfg = QoSConfig(aging_step_s=10.0, aging_max_boost=1)
    s = QoSScheduler(config=cfg, clock=clock)
    it = _make(clock, QoSClass.COLD, id="cold", enqueued_at=clock.now())
    s.admit(it)
    clock.advance(5.0)
    assert list(s.snapshot()["starving"]) == []  # type: ignore[call-overload]
    clock.advance(20.0)  # >= max_boost * step * 2 = 20s
    assert "cold" in list(s.snapshot()["starving"])  # type: ignore[call-overload]


# --------------------------------------------------------------------------- #
# deadline / EDF
# --------------------------------------------------------------------------- #


def test_urgency_and_expiry(clock: VirtualClock) -> None:
    cfg_horizon = 30.0
    near = _make(clock, QoSClass.COMMITTED, id="near", eta_s=10.0)
    far = _make(clock, QoSClass.COMMITTED, id="far", eta_s=120.0)
    late = _make(clock, QoSClass.COMMITTED, id="late", eta_s=-5.0)
    assert is_urgent(near, clock.now(), horizon_s=cfg_horizon)
    assert not is_urgent(far, clock.now(), horizon_s=cfg_horizon)
    assert is_expired(late, clock.now())
    assert urgency_score(near, clock.now(), horizon_s=cfg_horizon) > urgency_score(
        far, clock.now(), horizon_s=cfg_horizon
    )
    assert urgency_score(late, clock.now(), horizon_s=cfg_horizon) > 1.0


def test_edf_orders_soonest_deadline_first_within_class(clock: VirtualClock) -> None:
    horizon = 30.0
    soon = _make(clock, QoSClass.COMMITTED, id="soon", eta_s=5.0)
    later = _make(clock, QoSClass.COMMITTED, id="later", eta_s=20.0)
    far = _make(clock, QoSClass.COMMITTED, id="far", eta_s=300.0)
    ordered = sorted([far, later, soon], key=lambda it: edf_key(it, clock.now(), horizon_s=horizon))
    assert [it.id for it in ordered] == ["soon", "later", "far"]


def test_scheduler_dispatches_urgent_committed_before_far_committed(
    clock: VirtualClock,
) -> None:
    cfg = QoSConfig(total_slots=1, committed_reserved_slots=1, deadline_urgency_horizon_s=30.0)
    s = QoSScheduler(config=cfg, clock=clock)
    s.admit(_make(clock, QoSClass.COMMITTED, id="far", eta_s=300.0, book_id="book_a"))
    s.admit(_make(clock, QoSClass.COMMITTED, id="urgent", eta_s=3.0, book_id="book_a"))
    result = s.dispatch(available_slots=1)
    assert result.dispatched[0].id == "urgent"


# --------------------------------------------------------------------------- #
# load shedding (least-value-first)
# --------------------------------------------------------------------------- #


def test_shedding_never_drops_committed(clock: VirtualClock) -> None:
    cfg = QoSConfig(shed_target_depth=1, backpressure_depth=10)
    items = [
        _make(clock, QoSClass.COMMITTED, id="c1", value=1.0),
        _make(clock, QoSClass.COMMITTED, id="c2", value=1.0),
        _make(clock, QoSClass.COMMITTED, id="c3", value=1.0),
    ]
    victims = select_victims(items, clock.now(), config=cfg)
    assert victims == []  # committed is never a candidate


def test_shedding_drops_lowest_value_density_first(clock: VirtualClock) -> None:
    cfg = QoSConfig(shed_target_depth=2, backpressure_depth=10)
    high = _make(clock, QoSClass.SPECULATIVE, id="high", value=10.0, cost_s=2.0)  # density 5
    mid = _make(clock, QoSClass.SPECULATIVE, id="mid", value=6.0, cost_s=2.0)  # density 3
    low = _make(clock, QoSClass.COLD, id="low", value=1.0, cost_s=10.0)  # density 0.1
    victims = select_victims([high, mid, low], clock.now(), config=cfg, target_depth=2)
    # Need to shed 1 (3 items -> target 2); the lowest density goes first.
    assert [v.item.id for v in victims] == ["low"]
    assert victims[0].reason is SheddingReason.SHED_LEAST_VALUE


def test_shedding_drops_expired_before_anything_useful(clock: VirtualClock) -> None:
    cfg = QoSConfig(shed_target_depth=10, deadline_expiry_grace_s=1.0)
    expired = _make(clock, QoSClass.SPECULATIVE, id="expired", eta_s=-30.0, value=100.0, cost_s=1.0)
    useful = _make(clock, QoSClass.SPECULATIVE, id="useful", eta_s=20.0, value=1.0, cost_s=10.0)
    # Backlog (2) is below target (10), so only the expired item is reclaimed.
    victims = select_victims([expired, useful], clock.now(), config=cfg)
    assert [v.item.id for v in victims] == ["expired"]
    assert victims[0].reason is SheddingReason.SHED_OVER_DEADLINE


def test_scheduler_sheds_overload_on_dispatch(clock: VirtualClock, gen: LoadGen) -> None:
    cfg = QoSConfig(
        total_slots=2,
        committed_reserved_slots=0,
        backpressure_depth=100,
        speculation_slowdown_depth=99,
        shed_target_depth=3,
    )
    s = QoSScheduler(config=cfg, clock=clock)
    # 6 speculative of varying value; backlog 6 > shed_target 3 -> 3 shed.
    for i in range(6):
        s.admit(
            _make(
                clock,
                QoSClass.SPECULATIVE,
                id=f"s{i}",
                eta_s=120.0,
                value=float(i + 1),
                cost_s=5.0,
            )
        )
    result = s.dispatch(available_slots=2)
    assert len(result.shed) == 3
    # The lowest-value items (s0, s1, s2) were shed first.
    shed_ids = {v.item.id for v in result.shed}
    assert shed_ids == {"s0", "s1", "s2"}


# --------------------------------------------------------------------------- #
# per-book / per-tenant fairness
# --------------------------------------------------------------------------- #


def test_max_min_fair_equal_split() -> None:
    grant = max_min_fair_shares({"a": 10, "b": 10, "c": 10}, capacity=9)
    assert grant == {"a": 3, "b": 3, "c": 3}


def test_max_min_fair_redistributes_leftover() -> None:
    # 'a' only wants 1; its leftover slice flows to b and c.
    grant = max_min_fair_shares({"a": 1, "b": 10, "c": 10}, capacity=9)
    assert grant["a"] == 1
    assert grant["b"] + grant["c"] == 8
    assert abs(grant["b"] - grant["c"]) <= 1
    assert sum(grant.values()) == 9


def test_max_min_fair_scarce_capacity_distinct_books() -> None:
    grant = max_min_fair_shares({"a": 5, "b": 5, "c": 5}, capacity=2)
    # Two slots, three needy books -> two distinct books get one each.
    assert sum(grant.values()) == 2
    assert sum(1 for v in grant.values() if v == 1) == 2


def test_fair_book_allocation_one_book_cannot_starve_another(clock: VirtualClock) -> None:
    items = (
        [_make(clock, QoSClass.SPECULATIVE, id=f"a{i}", book_id="book_a") for i in range(20)]
        + [_make(clock, QoSClass.SPECULATIVE, id="b0", book_id="book_b")]
    )
    grant = fair_book_allocation(items, slots=4)
    # book_b (only 1 item) is not starved despite book_a flooding 20.
    assert grant["book_b"] == 1
    assert grant["book_a"] == 3
    assert starvation_free(grant, {"book_a": 20, "book_b": 1})


def test_scheduler_per_book_fairness_in_dispatch(clock: VirtualClock) -> None:
    cfg = QoSConfig(total_slots=4, committed_reserved_slots=0)
    s = QoSScheduler(config=cfg, clock=clock)
    for i in range(10):
        s.admit(_make(clock, QoSClass.COMMITTED, id=f"a{i}", book_id="book_a", eta_s=10.0))
    s.admit(_make(clock, QoSClass.COMMITTED, id="b0", book_id="book_b", eta_s=10.0))
    result = s.dispatch(available_slots=4)
    books = {it.book_id for it in result.dispatched}
    assert "book_b" in books  # the lone book_b shot is not starved by book_a's flood


def test_per_tenant_fairness_uses_tenant_over_book(clock: VirtualClock) -> None:
    # Two books under one tenant share that tenant's fair slice.
    items = [
        _make(clock, QoSClass.SPECULATIVE, id="t1a", book_id="book_a"),
        _make(clock, QoSClass.SPECULATIVE, id="t1b", book_id="book_b"),
        _make(clock, QoSClass.SPECULATIVE, id="t2c", book_id="book_c"),
    ]
    for it in items[:2]:
        it.tenant_id = "tenant_1"
    items[2].tenant_id = "tenant_2"
    grant = fair_book_allocation(items, slots=2)
    # Fairness is per tenant: tenant_1 (2 items) and tenant_2 (1 item) each get one.
    assert grant["tenant_1"] == 1
    assert grant["tenant_2"] == 1


# --------------------------------------------------------------------------- #
# integration: a saturation scenario over virtual time
# --------------------------------------------------------------------------- #


def test_end_to_end_saturation_committed_wins_cold_survives(clock: VirtualClock) -> None:
    cfg = QoSConfig(
        total_slots=6,
        committed_reserved_slots=4,
        backpressure_depth=64,
        speculation_slowdown_depth=48,
        shed_target_depth=56,
        aging_step_s=20.0,
        aging_max_boost=1,
    )
    s = QoSScheduler(config=cfg, clock=clock)
    gen = LoadGen(clock, seed=99)

    committed_served = 0
    cold_served = 0
    # 30 rounds: each round a steady trickle of committed + a flood of speculative
    # + a little cold; advance the clock; dispatch; complete everything started.
    for _ in range(30):
        for _ in range(3):
            s.admit(gen.item(qos_class=QoSClass.COMMITTED, eta_s=8.0, book_id="book_a"))
        for it in gen.burst(10, qos_class=QoSClass.SPECULATIVE, eta_low=30.0, eta_high=200.0):
            s.admit(it)
        for _ in range(2):
            s.admit(gen.item(qos_class=QoSClass.COLD, book_id="book_b"))
        result = s.dispatch(available_slots=6)
        committed_served += sum(1 for it in result.dispatched if it.qos_class is QoSClass.COMMITTED)
        cold_served += sum(1 for it in result.dispatched if it.qos_class is QoSClass.COLD)
        for it in result.dispatched:
            s.complete(it.id)
        clock.advance(5.0)

    # Committed (near-reader) work is consistently served; cold is never starved to
    # zero across the whole run (WFQ share + aging).
    assert committed_served >= 30 * 3 * 0.9  # nearly all committed cleared
    assert cold_served > 0
    # Backlog stayed bounded by backpressure (never grew unboundedly).
    assert s.depth <= cfg.backpressure_depth + 30  # admitted-but-not-dispatched ceiling


def test_burst_then_drain_no_thrash(clock: VirtualClock, gen: LoadGen) -> None:
    cfg = QoSConfig(total_slots=4, committed_reserved_slots=2)
    s = QoSScheduler(config=cfg, clock=clock)
    for it in gen.burst(8, qos_class=QoSClass.COMMITTED, eta_low=5.0, eta_high=40.0):
        s.admit(it)
    drained = 0
    for _ in range(3):
        result = s.dispatch(available_slots=4)
        drained += len(result.dispatched)
        for it in result.dispatched:
            s.complete(it.id)
    assert drained == 8  # all committed drains within a few rounds
    assert s.depth == 0


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #


class _FakeJob:
    """A structural stand-in for ``QueuedJob`` (the adapter is duck-typed)."""

    def __init__(
        self,
        *,
        id: str,
        priority: RenderPriority,
        book_id: str,
        session_id: str | None,
        target_word: int,
        target_duration_s: float,
        reserved_video_s: float,
    ) -> None:
        self.id = id
        self.priority = priority
        self.book_id = book_id
        self.session_id = session_id
        self.target_word = target_word
        self.target_duration_s = target_duration_s
        self.reserved_video_s = reserved_video_s


def test_adapter_lifts_job_with_reading_time_deadline() -> None:
    job = _FakeJob(
        id="job1",
        priority=RenderPriority.COMMITTED,
        book_id="book_a",
        session_id="sess",
        target_word=400,
        target_duration_s=5.0,
        reserved_video_s=5.0,
    )
    item = job_to_qos_item(job, now=1000.0, focus_word=0, velocity_wps=4.0)
    assert item.qos_class is QoSClass.COMMITTED
    assert item.book_id == "book_a"
    # ETA = (400 - 0) / 4 = 100s -> deadline 1100.
    assert item.deadline == pytest.approx(1100.0)
    assert item.eta_s == pytest.approx(100.0)


def test_adapter_no_focus_word_is_deadline_less() -> None:
    job = _FakeJob(
        id="job2",
        priority=RenderPriority.KEYFRAME,
        book_id="book_a",
        session_id=None,
        target_word=400,
        target_duration_s=5.0,
        reserved_video_s=0.0,
    )
    item = job_to_qos_item(job, now=1000.0)
    assert item.qos_class is QoSClass.COLD
    assert item.deadline is None
    assert item.slack_s(1000.0) is None
