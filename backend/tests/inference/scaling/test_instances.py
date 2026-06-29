"""Unit tests for the instance/cost model (app.inference.scaling.instances)."""

from __future__ import annotations

import math

import pytest

from app.inference.scaling.instances import (
    DEFAULT_CATALOG,
    BillingModel,
    CostBreakdown,
    InstanceType,
    catalog_by_name,
    default_catalog,
)


def test_cost_per_second_derives_from_hourly() -> None:
    inst = InstanceType(name="x", cost_per_hour=3600.0)
    assert inst.cost_per_second == pytest.approx(1.0)


def test_effective_service_time_applies_multiplier() -> None:
    fast = InstanceType(name="fast", cost_per_hour=6.0, service_time_multiplier=0.5)
    slow = InstanceType(name="slow", cost_per_hour=1.0, service_time_multiplier=2.0)
    assert fast.effective_service_time_s(10.0) == pytest.approx(5.0)
    assert slow.effective_service_time_s(10.0) == pytest.approx(20.0)


def test_throughput_accounts_for_concurrency_and_speed() -> None:
    inst = InstanceType(
        name="c2", cost_per_hour=2.0, service_time_multiplier=1.0, max_concurrency=2
    )
    # 2 concurrent / 5s each => 0.4 req/s.
    assert inst.throughput_per_s(5.0) == pytest.approx(0.4)


def test_cost_per_request_is_provisioned_slice() -> None:
    inst = InstanceType(
        name="c2", cost_per_hour=7200.0, service_time_multiplier=1.0, max_concurrency=2
    )
    # cost/s = 2.0; eff service = 5s; /concurrency 2 => 2*5/2 = 5.0.
    assert inst.cost_per_request(5.0) == pytest.approx(5.0)


def test_spot_survival_probability_is_exponential() -> None:
    spot = InstanceType(
        name="s",
        cost_per_hour=0.4,
        spot=True,
        reclaim_hazard_per_hour=3600.0,  # 1 per second
    )
    assert spot.reclaim_hazard_per_s() == pytest.approx(1.0)
    assert spot.survival_probability(0.0) == pytest.approx(1.0)
    assert spot.survival_probability(1.0) == pytest.approx(math.exp(-1.0))


def test_on_demand_never_reclaimed() -> None:
    inst = InstanceType(name="od", cost_per_hour=2.0)
    assert inst.survival_probability(1e6) == 1.0


def test_spot_must_declare_hazard() -> None:
    with pytest.raises(ValueError, match="reclaim hazard"):
        InstanceType(name="bad", cost_per_hour=0.4, spot=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cost_per_hour", -1.0),
        ("service_time_multiplier", 0.0),
        ("cold_start_s", -1.0),
        ("max_concurrency", 0),
        ("reclaim_hazard_per_hour", -1.0),
    ],
)
def test_invalid_instance_fields_raise(field: str, value: float) -> None:
    kwargs: dict[str, object] = {"name": "x", "cost_per_hour": 1.0}
    kwargs[field] = value
    with pytest.raises(ValueError):
        InstanceType(**kwargs)  # type: ignore[arg-type]


def test_default_catalog_spans_the_cost_speed_trade() -> None:
    cat = default_catalog()
    assert set(cat) == {"gpu-l20", "gpu-a10", "gpu-h20", "gpu-l20-spot"}
    # Cheap+slow vs dear+fast ordering holds.
    assert cat["gpu-l20"].cost_per_hour < cat["gpu-h20"].cost_per_hour
    assert cat["gpu-h20"].service_time_multiplier < cat["gpu-l20"].service_time_multiplier
    # Spot is the cheapest and is reclaimable.
    assert cat["gpu-l20-spot"].cost_per_hour == min(i.cost_per_hour for i in cat.values())
    assert cat["gpu-l20-spot"].spot


def test_default_catalog_constant_matches_factory() -> None:
    assert DEFAULT_CATALOG.keys() == default_catalog().keys()


def test_catalog_by_name_subsets_and_validates() -> None:
    sub = catalog_by_name(["gpu-l20", "gpu-h20"])
    assert set(sub) == {"gpu-l20", "gpu-h20"}
    with pytest.raises(KeyError, match="unknown instance"):
        catalog_by_name(["gpu-nonexistent"])


def test_cost_breakdown_amortises_per_request() -> None:
    cb = CostBreakdown(
        provisioned_cost=100.0,
        served_requests=50,
        window_s=3600.0,
        by_instance_type={"gpu-a10": 100.0},
        cold_start_cost=10.0,
        idle_cost=20.0,
    )
    assert cb.cost_per_request == pytest.approx(2.0)
    assert cb.total_cost == pytest.approx(100.0)
    d = cb.to_dict()
    assert d["served_requests"] == 50
    assert d["by_instance_type"] == {"gpu-a10": 100.0}


def test_cost_breakdown_zero_requests_is_infinite_per_request() -> None:
    cb = CostBreakdown(provisioned_cost=5.0, served_requests=0, window_s=10.0)
    assert cb.cost_per_request == math.inf
    assert cb.to_dict()["cost_per_request"] is None


def test_per_request_second_billing_model_enum() -> None:
    inst = InstanceType(
        name="faas", cost_per_hour=3600.0, billing=BillingModel.PER_REQUEST_SECOND
    )
    assert inst.billing is BillingModel.PER_REQUEST_SECOND
