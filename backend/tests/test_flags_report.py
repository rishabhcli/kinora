"""Experiment decision-report tests (pure)."""

from __future__ import annotations

from app.flags.defaults import crew_vs_baseline_experiment
from app.flags.experiment import ExperimentStatus
from app.flags.report import Recommendation, build_report
from app.flags.stats import ProportionStat


def _obs(
    base_ccs: int, crew_ccs: int, base_regen: int, crew_regen: int, n: int = 2000
) -> dict[str, dict[str, ProportionStat]]:
    return {
        "baseline": {
            "ccs_pass": ProportionStat(base_ccs, n),
            "regen_rate": ProportionStat(base_regen, n),
        },
        "crew": {
            "ccs_pass": ProportionStat(crew_ccs, n),
            "regen_rate": ProportionStat(crew_regen, n),
        },
    }


def test_ship_when_crew_clearly_wins_ccs_and_regen_ok() -> None:
    exp = crew_vs_baseline_experiment(status=ExperimentStatus.RUNNING)
    # crew passes CCS far more often; regen rate not worse
    report = build_report(exp, _obs(1200, 1700, 300, 280))
    assert report.recommendation is Recommendation.SHIP
    assert report.primary_metric == "ccs_pass"
    crew = next(c for c in report.comparisons if c.variant_key == "crew")
    assert crew.is_winner
    assert crew.relative_uplift > 0


def test_hold_when_inconclusive() -> None:
    exp = crew_vs_baseline_experiment()
    # nearly identical -> not decisive
    report = build_report(exp, _obs(1000, 1010, 300, 300))
    assert report.recommendation is Recommendation.HOLD


def test_rollback_when_guardrail_breached() -> None:
    exp = crew_vs_baseline_experiment()
    # crew wins CCS but regen rate (lower is better) blows way past the margin
    report = build_report(exp, _obs(1200, 1700, 300, 900))
    assert report.recommendation is Recommendation.ROLLBACK
    assert any(g.breached for g in report.guardrails)
    assert "guardrail" in report.rationale


def test_rollback_when_primary_regresses() -> None:
    exp = crew_vs_baseline_experiment()
    # crew clearly WORSE on CCS, regen fine -> decisive loss -> rollback
    report = build_report(exp, _obs(1700, 1100, 300, 300))
    assert report.recommendation is Recommendation.ROLLBACK


def test_report_to_dict_is_json_safe() -> None:
    import json

    exp = crew_vs_baseline_experiment()
    report = build_report(exp, _obs(1200, 1700, 300, 280))
    json.dumps(report.to_dict())  # must not raise
    d = report.to_dict()
    assert d["recommendation"] in {"ship", "hold", "rollback"}
    assert len(report.comparisons) == 1


def test_missing_observations_hold() -> None:
    exp = crew_vs_baseline_experiment()
    report = build_report(exp, {})  # no data at all
    assert report.recommendation is Recommendation.HOLD
    assert report.comparisons == ()
