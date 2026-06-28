"""Unit tests for the eval harness + A/B + regression detection (no infra)."""

from __future__ import annotations

import pytest

from app.llmops.ab import ABRunner
from app.llmops.datasets import get_dataset
from app.llmops.harness import EvalHarness, fake_responder, naive_responder
from app.llmops.regression import RegressionPolicy, detect, detect_from_harness


async def test_fake_responder_passes_adapter_dataset() -> None:
    ds = get_dataset("adapter_golden_v1")
    report = await EvalHarness().run(
        prompt_key="adapter", prompt_version="1.0.0", system="SYS", dataset=ds, runs=3
    )
    assert report.runs == 3
    assert report.mean_score > 0.7
    # The fake is deterministic -> zero run-to-run spread (honest signal).
    assert report.score_stdev == 0.0
    assert set(report.per_case_mean) == {c.id for c in ds.cases}


async def test_naive_responder_fails() -> None:
    ds = get_dataset("adapter_golden_v1")
    report = await EvalHarness().run(
        prompt_key="adapter",
        prompt_version="1.0.0",
        system="SYS",
        dataset=ds,
        responder=naive_responder,
        runs=2,
    )
    assert report.mean_score < 0.7
    assert report.mean_pass_rate < 1.0


async def test_safety_dataset_distinguishes_arms() -> None:
    ds = get_dataset("injection_probes_v1")
    safe = await EvalHarness().run(
        prompt_key="adapter",
        prompt_version="1.0.0",
        system="S",
        dataset=ds,
        responder=fake_responder,
    )
    unsafe = await EvalHarness().run(
        prompt_key="adapter",
        prompt_version="2.0.0",
        system="S",
        dataset=ds,
        responder=naive_responder,
    )
    assert safe.mean_pass_rate == 1.0
    assert unsafe.mean_pass_rate < 1.0


async def test_harness_rejects_zero_runs() -> None:
    ds = get_dataset("adapter_golden_v1")
    with pytest.raises(ValueError):
        await EvalHarness().run(
            prompt_key="adapter", prompt_version="1.0.0", system="S", dataset=ds, runs=0
        )


async def test_ab_picks_better_arm() -> None:
    ds = get_dataset("adapter_golden_v1")
    result = await ABRunner().compare(
        prompt_key="adapter",
        version_a="1.0.0",
        system_a="S",
        version_b="2.0.0",
        system_b="S",
        dataset=ds,
        responder_a=naive_responder,  # worse
        responder_b=fake_responder,  # better
    )
    assert result.winner == "B"
    assert result.mean_delta > 0
    assert result.wins_b >= 1


async def test_ab_tie_when_identical() -> None:
    ds = get_dataset("adapter_golden_v1")
    result = await ABRunner().compare(
        prompt_key="adapter",
        version_a="1.0.0",
        system_a="S",
        version_b="1.1.0",
        system_b="S",
        dataset=ds,
        responder_a=fake_responder,
        responder_b=fake_responder,
    )
    assert result.winner == "tie"
    assert abs(result.mean_delta) < 0.02


async def test_regression_detected_on_quality_drop() -> None:
    ds = get_dataset("adapter_golden_v1")
    harness = EvalHarness()
    baseline = await harness.run(
        prompt_key="adapter",
        prompt_version="1.0.0",
        system="S",
        dataset=ds,
        responder=fake_responder,
    )
    candidate = await harness.run(
        prompt_key="adapter",
        prompt_version="2.0.0",
        system="S",
        dataset=ds,
        responder=naive_responder,
    )
    verdict = detect(baseline, candidate)
    assert verdict.regressed
    assert verdict.overall_delta < 0
    kinds = {f.kind.value for f in verdict.findings}
    assert "overall_drop" in kinds


async def test_no_regression_when_equal() -> None:
    ds = get_dataset("adapter_golden_v1")
    verdict, base, cand = await detect_from_harness(
        prompt_key="adapter",
        baseline_version="1.0.0",
        baseline_system="S",
        candidate_version="1.1.0",
        candidate_system="S",
        dataset=ds,
        responder=fake_responder,
    )
    assert not verdict.regressed


async def test_regression_policy_tolerances_respected() -> None:
    ds = get_dataset("adapter_golden_v1")
    harness = EvalHarness()
    base = await harness.run(
        prompt_key="adapter",
        prompt_version="1.0.0",
        system="S",
        dataset=ds,
        responder=fake_responder,
    )
    cand = await harness.run(
        prompt_key="adapter",
        prompt_version="2.0.0",
        system="S",
        dataset=ds,
        responder=naive_responder,
    )
    # A very loose policy tolerates even the big drop.
    loose = RegressionPolicy(
        overall_tolerance=1.0, pass_rate_tolerance=1.0, criterion_tolerance=1.0, case_tolerance=1.0
    )
    assert not detect(base, cand, policy=loose).regressed
