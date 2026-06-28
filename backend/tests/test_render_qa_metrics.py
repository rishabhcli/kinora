"""The §13 eval-harness metrics — CCS, accepted-footage efficiency, regen, drift.

Builds shot outcomes directly (a QARecord projection) and verifies each metric plus
the crew-vs-baseline comparison §13 puts on the demo slide.
"""

from __future__ import annotations

from app.render.qa.metrics import (
    ShotOutcome,
    accepted_footage_efficiency,
    aggregate_runs,
    arm_metrics,
    character_ccs,
    compare_arms,
    regeneration_rate,
    style_drift_variance,
)


def _shot(
    sid: str,
    *,
    accepted: bool = True,
    dur: float = 5.0,
    ccs: float = 0.92,
    drift: float = 0.03,
    regens: int = 0,
    char: str | None = None,
) -> ShotOutcome:
    return ShotOutcome(
        shot_id=sid,
        accepted=accepted,
        duration_s=dur,
        ccs=ccs,
        style_drift=drift,
        regenerations=regens,
        character_key=char,
    )


# --------------------------------------------------------------------------- #
# Per-character CCS (§13: mean cos across the shots a character appears in)
# --------------------------------------------------------------------------- #


def test_character_ccs_groups_by_character() -> None:
    outcomes = [
        _shot("s1", ccs=0.90, char="hero"),
        _shot("s2", ccs=0.94, char="hero"),
        _shot("s3", ccs=0.80, char="villain"),
    ]
    result = {c.character_key: c for c in character_ccs(outcomes)}
    assert abs(result["hero"].mean_ccs - 0.92) < 1e-6
    assert result["hero"].n_shots == 2
    assert result["villain"].mean_ccs == 0.80


def test_character_ccs_ignores_unattributed_shots() -> None:
    assert character_ccs([_shot("s1", char=None)]) == []


# --------------------------------------------------------------------------- #
# Accepted-footage efficiency (§13 headline)
# --------------------------------------------------------------------------- #


def test_efficiency_all_accepted_is_100() -> None:
    eff, accepted, total = accepted_footage_efficiency([_shot("s1"), _shot("s2")])
    assert eff == 100.0
    assert accepted == total == 10.0


def test_efficiency_penalizes_rejected_footage() -> None:
    # 5s accepted of 10s generated → 50% efficiency.
    eff, accepted, total = accepted_footage_efficiency(
        [_shot("s1", accepted=True), _shot("s2", accepted=False)]
    )
    assert eff == 50.0
    assert accepted == 5.0
    assert total == 10.0


def test_efficiency_no_footage() -> None:
    eff, _, _ = accepted_footage_efficiency([])
    assert eff == 100.0


# --------------------------------------------------------------------------- #
# Regeneration rate + style-drift variance
# --------------------------------------------------------------------------- #


def test_regeneration_rate() -> None:
    outcomes = [_shot("s1", regens=1), _shot("s2", regens=0), _shot("s3", regens=1)]
    assert regeneration_rate(outcomes) == round(2 / 3, 4)


def test_style_drift_variance() -> None:
    # All equal drift → zero variance.
    assert style_drift_variance([_shot("s1", drift=0.05), _shot("s2", drift=0.05)]) == 0.0
    # Spread → positive variance.
    assert style_drift_variance([_shot("s1", drift=0.01), _shot("s2", drift=0.09)]) > 0.0


# --------------------------------------------------------------------------- #
# Arm metrics + crew-vs-baseline comparison (§13 chart)
# --------------------------------------------------------------------------- #


def test_arm_metrics_rolls_up() -> None:
    metrics = arm_metrics(
        "crew",
        [_shot("s1", ccs=0.95, char="hero"), _shot("s2", ccs=0.85, accepted=False, char="hero")],
    )
    assert metrics.arm == "crew"
    assert metrics.n_shots == 2
    assert abs(metrics.mean_ccs - 0.90) < 1e-6
    assert metrics.accepted_footage_efficiency == 50.0


def test_compare_arms_shows_crew_winning() -> None:
    # Crew: high CCS, no rejects, no regens, coherent. Baseline: worse on all.
    # Crew holds a steady look (low style-drift variance); the baseline's look
    # wanders shot-to-shot (high variance) — exactly what §13 measures.
    crew = arm_metrics(
        "crew",
        [_shot(f"c{i}", ccs=0.94, drift=0.03, regens=0, char="hero") for i in range(5)],
    )
    baseline = arm_metrics(
        "baseline",
        [
            _shot(
                f"b{i}",
                ccs=0.80,
                drift=0.02 + 0.04 * i,  # 0.02, 0.06, 0.10, 0.14, 0.18 → high variance
                regens=1,
                accepted=(i % 2 == 0),
                char="hero",
            )
            for i in range(5)
        ],
    )
    cmp = compare_arms(crew, baseline)
    assert cmp.ccs_gain > 0  # crew more consistent
    assert cmp.efficiency_gain_pct > 0  # crew burns less budget
    assert cmp.regen_reduction > 0  # crew regenerates less
    assert cmp.style_drift_reduction > 0  # crew's look more coherent


# --------------------------------------------------------------------------- #
# Multi-run aggregation (§13: mean ± spread across 3 runs)
# --------------------------------------------------------------------------- #


def test_aggregate_runs_mean_and_spread() -> None:
    stats = aggregate_runs([0.90, 0.92, 0.94])
    assert abs(stats.mean - 0.92) < 1e-6
    assert stats.stdev > 0
    assert stats.n == 3


def test_aggregate_runs_single() -> None:
    stats = aggregate_runs([0.91])
    assert stats.mean == 0.91
    assert stats.stdev == 0.0
