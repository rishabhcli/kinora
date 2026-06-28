"""Experiment assignment + exposure tests."""

from __future__ import annotations

import pytest

from app.flags.context import EvalContext
from app.flags.errors import ExperimentValidationError
from app.flags.experiment import (
    Experiment,
    ExperimentEngine,
    ExperimentStatus,
    Metric,
    MetricDirection,
    MetricKind,
    Variant,
    expected_allocation,
)
from app.flags.models import Clause, Operator, Rule


def make_exp(**kw: object) -> Experiment:
    defaults: dict[str, object] = {
        "key": "exp",
        "variants": (Variant("control", 5000, is_control=True), Variant("treatment", 5000)),
        "salt": "exp-salt",
        "status": ExperimentStatus.RUNNING,
    }
    defaults.update(kw)
    return Experiment(**defaults)  # type: ignore[arg-type]


def test_requires_two_variants() -> None:
    with pytest.raises(ExperimentValidationError):
        Experiment("e", (Variant("a", 10_000, is_control=True),), salt="s")


def test_weights_must_sum() -> None:
    with pytest.raises(ExperimentValidationError):
        Experiment(
            "e",
            (Variant("a", 4000, is_control=True), Variant("b", 5000)),
            salt="s",
        )


def test_exactly_one_control() -> None:
    with pytest.raises(ExperimentValidationError):
        Experiment(
            "e",
            (Variant("a", 5000), Variant("b", 5000)),
            salt="s",
        )
    with pytest.raises(ExperimentValidationError):
        Experiment(
            "e",
            (Variant("a", 5000, is_control=True), Variant("b", 5000, is_control=True)),
            salt="s",
        )


def test_assignment_split_is_proportional() -> None:
    exp = make_exp(
        variants=(
            Variant("control", 6000, is_control=True),
            Variant("treatment", 4000),
        )
    )
    eng = ExperimentEngine(exp)
    arms = [eng.assign(EvalContext.of(f"u{i}")).variant_key for i in range(10_000)]
    assert 0.57 < arms.count("control") / 10_000 < 0.63
    assert 0.37 < arms.count("treatment") / 10_000 < 0.43


def test_assignment_is_deterministic() -> None:
    eng = ExperimentEngine(make_exp())
    for i in range(200):
        c = EvalContext.of(f"u{i}")
        assert eng.assign(c).variant_key == eng.assign(c).variant_key


def test_draft_and_concluded_do_not_assign() -> None:
    for status in (ExperimentStatus.DRAFT, ExperimentStatus.CONCLUDED):
        eng = ExperimentEngine(make_exp(status=status))
        a = eng.assign(EvalContext.of("u"))
        assert not a.in_experiment
        assert a.reason == "not_running"


def test_audience_gating() -> None:
    exp = make_exp(
        audience=(Rule("pro", (Clause("plan", Operator.EQ, ("pro",)),), variation="x"),),
    )
    eng = ExperimentEngine(exp)
    assert eng.assign(EvalContext.of("u", plan="pro")).in_experiment
    excluded = eng.assign(EvalContext.of("u", plan="free"))
    assert not excluded.in_experiment
    assert excluded.reason == "audience_excluded"


def test_traffic_percent_ramps_enrollment() -> None:
    eng = ExperimentEngine(make_exp(traffic_percent=20.0))
    enrolled = sum(
        1 for i in range(10_000) if eng.assign(EvalContext.of(f"u{i}")).in_experiment
    )
    assert 0.17 < enrolled / 10_000 < 0.23


def test_enrolled_arm_split_unbiased_by_traffic() -> None:
    # Among the 20% enrolled, the control/treatment split should still be ~50/50.
    eng = ExperimentEngine(make_exp(traffic_percent=20.0))
    arms = [
        a.variant_key
        for i in range(20_000)
        if (a := eng.assign(EvalContext.of(f"u{i}"))).in_experiment
    ]
    frac_control = arms.count("control") / len(arms)
    assert 0.46 < frac_control < 0.54


def test_exposure_key_stable_and_none_when_excluded() -> None:
    eng = ExperimentEngine(make_exp())
    c = EvalContext.of("u1")
    a = eng.assign(c)
    key = eng.exposure_key(c, a)
    assert key is not None
    assert key == eng.exposure_key(c, a)  # stable
    # not enrolled -> no key
    draft = ExperimentEngine(make_exp(status=ExperimentStatus.DRAFT))
    assert draft.exposure_key(c, draft.assign(c)) is None


def test_exposure_key_none_for_anonymous() -> None:
    eng = ExperimentEngine(make_exp())
    anon = EvalContext(key="anon", anonymous=True)
    a = eng.assign(anon)
    # anonymous still buckets, but no durable exposure
    assert eng.exposure_key(anon, a) is None


def test_exposure_key_changes_with_version() -> None:
    c = EvalContext.of("u1")
    e1 = ExperimentEngine(make_exp(version=1))
    e2 = ExperimentEngine(make_exp(version=2))
    assert e1.exposure_key(c, e1.assign(c)) != e2.exposure_key(c, e2.assign(c))


def test_metric_helpers() -> None:
    primary = Metric("converted", kind=MetricKind.PROPORTION)
    guard = Metric(
        "stalls", direction=MetricDirection.DECREASE, is_guardrail=True, guardrail_margin=0.02
    )
    exp = make_exp(metrics=(primary, guard))
    assert exp.primary_metric is primary
    assert exp.guardrails == (guard,)
    assert exp.control.key == "control"


def test_duplicate_metric_keys_rejected() -> None:
    with pytest.raises(ExperimentValidationError):
        make_exp(metrics=(Metric("m"), Metric("m")))


def test_expected_allocation() -> None:
    exp = make_exp(
        variants=(Variant("control", 2500, is_control=True), Variant("t", 7500))
    )
    alloc = expected_allocation(exp)
    assert alloc == {"control": 0.25, "t": 0.75}


def test_bucket_by_secondary_unit() -> None:
    # bucket by tenant so all users in a tenant share an arm
    exp = make_exp(bucket_by="tenant")
    eng = ExperimentEngine(exp)
    a1 = eng.assign(EvalContext(key="user-a", attributes={"tenant": "acme"}))
    a2 = eng.assign(EvalContext(key="user-b", attributes={"tenant": "acme"}))
    assert a1.variant_key == a2.variant_key  # tenant-consistent
