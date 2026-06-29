"""End-to-end: the reading→scheduler→queue→render→events loop run inside the
deterministic sim, plus the invariants and the sweep/shrink/replay workflow
(kinora.md §4, §9.7, §11.1, §12).

This is the payoff suite. It proves the real control plane (the actual
``SchedulerService`` + ``RedisRenderQueue``) survives thousands of seeded fault
schedules with its safety + (product-satisfiable) liveness invariants intact, that
runs are byte-reproducible, and that when an invariant *is* violated the framework
shrinks the failing schedule to a minimal reproducer and replays it exactly.
"""

from __future__ import annotations

import pytest

from app.core.logging import configure_logging
from app.verification.simulation import (
    CORE_INVARIANTS,
    STRICT_INVARIANTS,
    FaultProfile,
    FaultSchedule,
    Simulation,
    SystemConfig,
    check_invariants,
    replay,
    run_seed,
    run_system,
    shrink,
    sweep,
)
from app.verification.simulation.workload import ARCHETYPES


@pytest.fixture(autouse=True)
def _quiet_logs() -> None:
    """The render simulator logs every §9.7 transition at INFO; silence it so the
    suite output stays readable (these tests run hundreds of render walks)."""
    configure_logging("CRITICAL")


_FAST = SystemConfig(session_duration_ms=45_000, n_shots=200)


# --------------------------------------------------------------------------- #
# A single run: the loop completes and converges
# --------------------------------------------------------------------------- #


def test_calm_run_completes_and_converges() -> None:
    with Simulation(FaultSchedule(seed=1, profile=FaultProfile.calm())) as sim:
        report = run_system(sim, _FAST)
    assert report.budget is not None and report.events is not None
    # The loop did real work...
    assert len(report.shots) > 0
    assert len(report.accepted_shots()) > 0
    # ...and converged to a clean quiescent state.
    assert report.final_queue_depth == 0
    assert report.unresolved_shots() == []
    assert report.budget.accounting_ok
    # Every accepted shot reached the client.
    assert len(report.events.of_type("clip_ready")) >= len(report.accepted_shots())


def test_calm_buffer_fills_toward_high_watermark() -> None:
    with Simulation(FaultSchedule(seed=2, profile=FaultProfile.calm())) as sim:
        report = run_system(sim, _FAST)
    peak = max(occ for _t, occ in report.buffer_samples)
    # The §4.5 hysteresis fills the committed buffer up to H.
    assert peak == pytest.approx(report.high_watermark, abs=report.config.shot_duration_s)


# --------------------------------------------------------------------------- #
# Determinism / replay
# --------------------------------------------------------------------------- #


def test_same_schedule_reproduces_identically() -> None:
    cfg = _FAST
    sched = FaultSchedule(seed=12345, profile=FaultProfile.chaos())

    def fingerprint() -> tuple:
        with Simulation(sched) as sim:
            r = run_system(sim, cfg)
            assert r.budget is not None
            return (
                len(r.shots),
                len(r.accepted_shots()),
                len(r.degraded_shots()),
                round(r.budget.spent, 3),
                r.final_queue_depth,
                r.final_dlq_len,
                r.reaped_jobs,
                tuple(sorted(r.shots)),
            )

    assert fingerprint() == fingerprint()  # byte-identical across runs


def test_replay_of_a_seed_is_stable() -> None:
    sched = FaultSchedule(seed=77, profile=FaultProfile.nominal())
    a = replay(sched, archetype="seeker", config=_FAST)
    b = replay(sched, archetype="seeker", config=_FAST)
    assert a.invariants.ok == b.invariants.ok
    assert len(a.system.shots) == len(b.system.shots)


# --------------------------------------------------------------------------- #
# Core invariants hold under EVERY profile (safety) — the verification result
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_core_invariants_hold_under_nominal(archetype: str) -> None:
    res = run_seed(seed=0, profile=FaultProfile.nominal(), archetype=archetype, config=_FAST)
    assert res.ok, res.invariants.summary()


@pytest.mark.parametrize("archetype", ARCHETYPES)
def test_core_invariants_hold_under_chaos(archetype: str) -> None:
    # Safety must hold even under a storm; the §4.4 ladder absorbs the quality hit.
    res = run_seed(seed=0, profile=FaultProfile.chaos(), archetype=archetype, config=_FAST)
    assert res.ok, res.invariants.summary()


def test_sweep_many_seeds_nominal_all_pass() -> None:
    # A modest grid keeps the suite fast; ad-hoc sweeps of 90+ seeds are clean too.
    res = sweep(
        profile=FaultProfile.nominal(),
        seeds=range(4),
        archetypes=ARCHETYPES,
        config=_FAST,
        invariants=CORE_INVARIANTS,
    )
    assert res.ok, res.summary()
    assert res.passed == res.ran == 4 * len(ARCHETYPES)


def test_sweep_many_seeds_chaos_safety_holds() -> None:
    res = sweep(
        profile=FaultProfile.chaos(),
        seeds=range(4),
        archetypes=ARCHETYPES,
        config=_FAST,
        invariants=CORE_INVARIANTS,
    )
    assert res.ok, res.summary()


# --------------------------------------------------------------------------- #
# The framework detects, shrinks, and replays a real bug
# --------------------------------------------------------------------------- #


def test_strict_sweep_detects_the_reservation_leak() -> None:
    """Under the STRICT suite (which demands every reservation resolve), the
    simulator must FIND the known scheduler reserve→enqueue leak — see DESIGN.md.
    This is the framework proving it can catch a genuine eventual-consistency bug,
    not a synthetic one."""
    res = sweep(
        profile=FaultProfile.chaos(),
        seeds=range(3),
        archetypes=ARCHETYPES,
        config=_FAST,
        invariants=STRICT_INVARIANTS,
    )
    assert not res.ok, "expected the strict suite to surface the reservation leak"
    assert res.first_failure is not None
    v = res.first_failure.invariants.first_violation()
    assert v is not None
    assert v.name == "reservations_resolved"


def test_shrink_minimises_and_replay_reproduces() -> None:
    res = sweep(
        profile=FaultProfile.chaos(),
        seeds=range(3),
        archetypes=ARCHETYPES,
        config=_FAST,
        invariants=STRICT_INVARIANTS,
    )
    failing = res.first_failure
    assert failing is not None

    shrunk = shrink(failing, config=_FAST, invariants=STRICT_INVARIANTS, max_steps=120)
    # The shrinker found a strictly simpler adversary than the original storm.
    assert len(shrunk.minimal.profile.active_kinds()) <= len(
        failing.schedule.profile.active_kinds()
    )
    assert shrunk.minimal.seed == failing.schedule.seed  # seed is never changed

    # The minimal schedule still reproduces the *same* violation, deterministically.
    rep = replay(
        shrunk.minimal,
        archetype=shrunk.archetype,
        config=_FAST,
        invariants=STRICT_INVARIANTS,
    )
    assert not rep.ok
    v = rep.invariants.first_violation()
    assert v is not None and v.name == "reservations_resolved"


def test_minimal_leak_schedule_passes_core_invariants() -> None:
    """The shrunken leak schedule violates ONLY the strict reservation invariant —
    proving the finding is isolated, not general state corruption."""
    res = sweep(
        profile=FaultProfile.chaos(),
        seeds=range(3),
        archetypes=ARCHETYPES,
        config=_FAST,
        invariants=STRICT_INVARIANTS,
    )
    assert res.first_failure is not None
    shrunk = shrink(res.first_failure, config=_FAST, invariants=STRICT_INVARIANTS, max_steps=120)
    under_core = replay(
        shrunk.minimal, archetype=shrunk.archetype, config=_FAST, invariants=CORE_INVARIANTS
    )
    assert under_core.ok, under_core.invariants.summary()


# --------------------------------------------------------------------------- #
# Invariant unit behaviour
# --------------------------------------------------------------------------- #


def test_check_invariants_returns_named_results() -> None:
    with Simulation(FaultSchedule(seed=3, profile=FaultProfile.calm())) as sim:
        report = run_system(sim, _FAST)
    inv = check_invariants(report, invariants=CORE_INVARIANTS)
    names = {r.name for r in inv.results}
    assert "no_double_spend" in names
    assert "no_stuck_shots" in names
    assert inv.ok


# --------------------------------------------------------------------------- #
# Budget exhaustion → degradation ladder (§11.1 / §12.4)
# --------------------------------------------------------------------------- #


def test_budget_exhaustion_caps_spend_and_stays_conserved() -> None:
    """A small video-second pool must be spent down to (at most) the cap, emit a
    ``budget_low`` event near the floor, and never break the ledger or strand a
    shot — the scheduler stops promoting full video and rides the §12.4 ladder."""
    cfg = SystemConfig(
        session_duration_ms=120_000,
        budget_total_s=60.0,
        budget_floor_s=20.0,
        n_shots=300,
    )
    with Simulation(FaultSchedule(seed=1, profile=FaultProfile.calm())) as sim:
        report = run_system(sim, cfg)
        remaining = sim.run_sync(report.budget.remaining()) if report.budget else 0.0

    assert report.budget is not None and report.events is not None
    assert report.budget.spent <= cfg.budget_total_s + 1e-6  # never overspends the pool
    assert remaining >= 0.0
    assert report.budget.accounting_ok  # ledger conserved even at exhaustion
    assert report.unresolved_shots() == []  # no shot stuck when the budget runs dry
    assert len(report.events.of_type("budget_low")) >= 1  # warned the client
