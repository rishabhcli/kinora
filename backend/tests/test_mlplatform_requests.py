"""Unit + property tests for the workload model (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.errors import ServingConfigError
from app.mlplatform.serving.requests import (
    InferenceRequest,
    RequestState,
    WorkloadGenerator,
)


def test_request_validation() -> None:
    for bad in (
        {"prompt_tokens": 0},
        {"max_tokens": 0},
        {"gen_tokens": 0},
        {"arrival_ms": -1.0},
    ):
        kw = {
            "request_id": "r",
            "arrival_ms": 0.0,
            "prompt_tokens": 4,
            "max_tokens": 4,
            "gen_tokens": 4,
        }
        kw.update(bad)
        with pytest.raises(ServingConfigError):
            InferenceRequest(**kw)  # type: ignore[arg-type]


def test_request_token_math() -> None:
    r = InferenceRequest("r", 0.0, prompt_tokens=10, max_tokens=20, gen_tokens=8)
    assert r.target_tokens == 8  # min(max, gen)
    assert r.remaining_tokens == 8
    r.generated = 3
    assert r.remaining_tokens == 5
    assert r.total_context_tokens == 13
    r.generated = 8
    assert r.remaining_tokens == 0
    assert not r.is_terminal
    r.state = RequestState.DONE
    assert r.is_terminal


def test_request_max_tokens_caps_generation() -> None:
    r = InferenceRequest("r", 0.0, prompt_tokens=10, max_tokens=5, gen_tokens=100)
    assert r.target_tokens == 5


def test_sort_key_orders_by_priority_then_arrival_then_id() -> None:
    a = InferenceRequest("a", 5.0, 4, 4, 4, priority=1)
    b = InferenceRequest("b", 1.0, 4, 4, 4, priority=0)
    c = InferenceRequest("c", 1.0, 4, 4, 4, priority=0)
    ordered = sorted([a, b, c], key=lambda r: r.sort_key())
    assert [r.request_id for r in ordered] == ["b", "c", "a"]


def test_generator_validation() -> None:
    with pytest.raises(ServingConfigError):
        WorkloadGenerator(arrival_rate_per_s=0.0)
    with pytest.raises(ServingConfigError):
        WorkloadGenerator(mean_prompt_tokens=0)
    with pytest.raises(ServingConfigError):
        WorkloadGenerator(priority_levels=0)


def test_generator_is_deterministic() -> None:
    g = WorkloadGenerator(seed="kinora", n_requests=50)
    a = g.generate()
    b = g.generate()
    assert [r.request_id for r in a] == [r.request_id for r in b]
    assert [r.prompt_tokens for r in a] == [r.prompt_tokens for r in b]
    assert [r.arrival_ms for r in a] == [r.arrival_ms for r in b]


def test_generator_arrivals_monotonic_nondecreasing() -> None:
    reqs = WorkloadGenerator(seed="s", n_requests=100).generate()
    arrivals = [r.arrival_ms for r in reqs]
    assert arrivals == sorted(arrivals)


def test_generator_empty() -> None:
    assert WorkloadGenerator(n_requests=0).generate() == []


@pytest.mark.parametrize("seed", [f"seed-{i}" for i in range(20)])
def test_property_generated_requests_are_well_formed(seed: str) -> None:
    """For any seed, every generated request satisfies its construction invariants."""
    g = WorkloadGenerator(
        seed=seed,
        n_requests=80,
        mean_prompt_tokens=400,
        prompt_spread=200,
        mean_gen_tokens=120,
        gen_spread=80,
        max_tokens=256,
        priority_levels=3,
    )
    for r in g.generate():
        assert r.prompt_tokens >= 8
        assert r.gen_tokens >= 1
        assert r.gen_tokens <= r.max_tokens
        assert r.arrival_ms >= 0.0
        assert 0 <= r.priority < 3
        assert r.state == RequestState.QUEUED
