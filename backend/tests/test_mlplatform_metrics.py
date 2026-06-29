"""Unit tests for the serving-run metrics (no infra)."""

from __future__ import annotations

import pytest

from app.mlplatform.serving.metrics import (
    LatencyStats,
    RunAccumulator,
    percentile,
    summarize_run,
)
from app.mlplatform.serving.requests import InferenceRequest, RequestState


def test_percentile_nearest_rank() -> None:
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(data, 0) == 10.0
    assert percentile(data, 100) == 50.0
    assert percentile(data, 50) == 30.0
    assert percentile(data, 90) == 50.0


def test_percentile_empty_and_bounds() -> None:
    assert percentile([], 50) == 0.0
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_latency_stats_of_empty() -> None:
    s = LatencyStats.of([])
    assert s.mean == s.p50 == s.p99 == s.max == 0.0


def test_latency_stats_of_values() -> None:
    s = LatencyStats.of([1.0, 2.0, 3.0, 4.0])
    assert s.mean == pytest.approx(2.5)
    assert s.max == 4.0
    d = s.as_dict()
    assert set(d) == {"mean_ms", "p50_ms", "p90_ms", "p99_ms", "max_ms"}


def _done(
    rid: str, arrival: float, ftt: float, finish: float, prompt: int, gen: int
) -> InferenceRequest:
    r = InferenceRequest(rid, arrival, prompt_tokens=prompt, max_tokens=gen, gen_tokens=gen)
    r.state = RequestState.DONE
    r.first_token_ms = ftt
    r.finish_ms = finish
    r.generated = gen
    return r


def test_summarize_run_basic_aggregates() -> None:
    completed = [
        _done("a", arrival=0.0, ftt=10.0, finish=100.0, prompt=50, gen=20),
        _done("b", arrival=0.0, ftt=20.0, finish=200.0, prompt=50, gen=20),
    ]
    acc = RunAccumulator()
    acc.observe_step(2, 0.5)
    acc.observe_step(1, 0.25)
    report = summarize_run(
        completed,
        [],
        cost_per_1k_tokens=2.0,
        wall_clock_ms=200.0,
        accumulator=acc,
        kv_reuse_ratio=0.3,
        speculative_speedup=1.8,
    )
    assert report.n_completed == 2
    assert report.n_failed == 0
    assert report.total_prompt_tokens == 100
    assert report.total_generated_tokens == 40
    # 140 tokens over 0.2s
    assert report.tokens_per_s == pytest.approx(140 / 0.2)
    # cost = 140/1000 * 2.0
    assert report.total_cost == pytest.approx(0.28)
    assert report.mean_batch_occupancy == pytest.approx(1.5)
    assert report.peak_batch_occupancy == 2
    assert report.peak_kv_utilization == 0.5
    assert report.kv_reuse_ratio == 0.3
    assert report.speculative_speedup == 1.8
    assert report.sim_steps == 2
    # TTFT = first_token - arrival
    assert report.ttft.max == 20.0


def test_summarize_run_empty() -> None:
    report = summarize_run([], [], cost_per_1k_tokens=1.0, wall_clock_ms=0.0)
    assert report.n_completed == 0
    assert report.tokens_per_s == 0.0
    assert report.cost_per_request == 0.0
    d = report.as_dict()
    assert d["n_completed"] == 0


def test_report_as_dict_is_json_friendly() -> None:
    report = summarize_run(
        [_done("a", 0.0, 5.0, 50.0, 10, 10)],
        [],
        cost_per_1k_tokens=1.0,
        wall_clock_ms=50.0,
    )
    d = report.as_dict()
    assert isinstance(d["ttft"], dict)
    assert isinstance(d["tokens_per_s"], float)
