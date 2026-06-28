"""InMemoryFlagService tests — the infra-free service surface."""

from __future__ import annotations

import pytest

from app.flags.context import EvalContext
from app.flags.experiment import Experiment, ExperimentStatus, Variant
from app.flags.models import Flag, Reason
from app.flags.service import build_local_service

pytestmark = pytest.mark.asyncio


async def test_upsert_and_evaluate() -> None:
    svc = build_local_service(default_salt="kinora")
    await svc.upsert_flag(Flag.boolean("live-video", enabled=True, rollout_percent=100.0))
    ev = await svc.evaluate("live-video", EvalContext.of("u"))
    assert ev.value is True
    assert ev.reason is Reason.FALLTHROUGH


async def test_evaluate_missing_returns_default() -> None:
    svc = build_local_service()
    ev = await svc.evaluate("ghost", EvalContext.of("u"), default="fallback")
    assert ev.value == "fallback"
    assert ev.reason is Reason.FLAG_NOT_FOUND


async def test_upsert_bumps_version() -> None:
    svc = build_local_service()
    a = await svc.upsert_flag(Flag.boolean("x"))
    b = await svc.upsert_flag(Flag.boolean("x"))
    assert a.version == 1
    assert b.version == 2


async def test_experiment_assign_and_exposure_counts() -> None:
    svc = build_local_service(default_salt="kinora")
    exp = Experiment(
        key="ab",
        variants=(Variant("control", 5000, is_control=True), Variant("treatment", 5000)),
        salt="ab-salt",
        status=ExperimentStatus.RUNNING,
    )
    await svc.upsert_experiment(exp)
    for i in range(400):
        await svc.assign("ab", EvalContext.of(f"u{i}"))
    # repeat some units -> idempotent exposures
    for i in range(50):
        await svc.assign("ab", EvalContext.of(f"u{i}"))
    counts = await svc.exposure_counts("ab")
    assert sum(counts.values()) == 400  # deduped
    assert set(counts) <= {"control", "treatment"}


async def test_assign_unknown_experiment_returns_none() -> None:
    svc = build_local_service()
    assert await svc.assign("nope", EvalContext.of("u")) is None


async def test_decide_experiment() -> None:
    from app.flags.defaults import crew_vs_baseline_experiment

    svc = build_local_service()
    await svc.upsert_experiment(crew_vs_baseline_experiment(status=ExperimentStatus.RUNNING))
    report = await svc.decide_experiment(
        "crew-vs-baseline",
        {
            "baseline": {
                "ccs_pass": {"successes": 1200, "trials": 2000},
                "regen_rate": {"successes": 300, "trials": 2000},
            },
            "crew": {
                "ccs_pass": {"successes": 1700, "trials": 2000},
                "regen_rate": {"successes": 280, "trials": 2000},
            },
        },
    )
    assert report is not None
    assert report["recommendation"] == "ship"


async def test_decide_unknown_experiment_none() -> None:
    svc = build_local_service()
    assert await svc.decide_experiment("nope", {}) is None


async def test_anonymous_not_logged() -> None:
    svc = build_local_service()
    exp = Experiment(
        key="ab",
        variants=(Variant("c", 5000, is_control=True), Variant("t", 5000)),
        salt="s",
        status=ExperimentStatus.RUNNING,
    )
    await svc.upsert_experiment(exp)
    await svc.assign("ab", EvalContext(key="anon", anonymous=True))
    assert await svc.exposure_counts("ab") == {}
