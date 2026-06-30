"""Pure statistics: pinned against hand-computed / table values, fully deterministic."""

from __future__ import annotations

import math

import pytest

from app.video.shadow import stats

# --- special functions vs known values ----------------------------------- #


def test_normal_cdf_known_points() -> None:
    assert stats.normal_cdf(0.0) == pytest.approx(0.5)
    assert stats.normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert stats.normal_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_student_t_cdf_table_value() -> None:
    # df=9, t=2.262 is the classic 97.5% one-sided critical value.
    assert stats.student_t_cdf(2.262, 9) == pytest.approx(0.975, abs=1e-3)


def test_student_t_quantile_inverts_cdf() -> None:
    for df in (1, 5, 9, 30):
        q = stats.student_t_quantile(0.975, df)
        assert stats.student_t_cdf(q, df) == pytest.approx(0.975, abs=1e-4)


def test_incomplete_beta_symmetry() -> None:
    # I_x(a,b) = 1 - I_{1-x}(b,a)
    a, b, x = 2.0, 3.0, 0.4
    assert stats.incomplete_beta(a, b, x) == pytest.approx(
        1.0 - stats.incomplete_beta(b, a, 1.0 - x), abs=1e-9
    )


# --- paired t-test --------------------------------------------------------- #


def test_paired_t_test_constant_positive_delta() -> None:
    # All deltas identical and positive: a degenerate, certain improvement.
    result = stats.paired_t_test([0.2, 0.2, 0.2, 0.2])
    assert result.mean == pytest.approx(0.2)
    assert result.std == 0.0
    assert result.p_value == 0.0
    assert result.interval.low == pytest.approx(0.2)
    assert result.interval.is_positive


def test_paired_t_test_zero_mean_is_inconclusive() -> None:
    result = stats.paired_t_test([0.1, -0.1, 0.1, -0.1])
    assert result.mean == pytest.approx(0.0)
    # Symmetric around zero → not significant, CI straddles zero.
    assert result.p_value > 0.5
    assert result.interval.low < 0.0 < result.interval.high
    assert not result.interval.excludes_zero


def test_paired_t_test_matches_manual_computation() -> None:
    diffs = [0.1, 0.2, 0.15, 0.05, 0.25]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    std = math.sqrt(var)
    se = std / math.sqrt(n)
    t = mean / se
    result = stats.paired_t_test(diffs)
    assert result.mean == pytest.approx(mean)
    assert result.std == pytest.approx(std)
    assert result.t_statistic == pytest.approx(t)
    assert result.df == n - 1


def test_paired_t_test_needs_two_points() -> None:
    with pytest.raises(ValueError):
        stats.paired_t_test([0.1])


# --- win-rate + Wilson ----------------------------------------------------- #


def test_win_rate_counts_wins_losses_ties_with_margin() -> None:
    deltas = [0.3, -0.2, 0.01, -0.01, 0.0, 0.5]
    wr = stats.win_rate(deltas, margin=0.05)
    # > 0.05: 0.3, 0.5 → 2 wins; < -0.05: -0.2 → 1 loss; rest ties (0.01,-0.01,0).
    assert wr.wins == 2
    assert wr.losses == 1
    assert wr.ties == 3
    assert wr.n == 6
    assert wr.rate == pytest.approx(2 / 6)


def test_win_rate_all_wins_is_one() -> None:
    wr = stats.win_rate([0.1, 0.2, 0.3])
    assert wr.rate == 1.0
    assert wr.wins == 3


def test_wilson_interval_brackets_estimate() -> None:
    ci = stats.wilson_interval(8, 10)
    assert ci.low < 0.8 < ci.high
    assert 0.0 <= ci.low <= ci.high <= 1.0


def test_wilson_interval_extremes_stay_in_unit() -> None:
    ci0 = stats.wilson_interval(0, 10)
    ci1 = stats.wilson_interval(10, 10)
    assert ci0.low == 0.0
    assert ci1.high == 1.0


# --- Wilcoxon signed-rank -------------------------------------------------- #


def test_wilcoxon_all_positive_is_significant() -> None:
    result = stats.wilcoxon_signed_rank([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    assert result.n == 6
    assert result.method == "exact"
    # All positive → statistic (min of W+/W-) is 0 → strong two-sided evidence.
    assert result.statistic == 0.0
    assert result.p_value < 0.05


def test_wilcoxon_symmetric_is_not_significant() -> None:
    result = stats.wilcoxon_signed_rank([0.1, -0.1, 0.2, -0.2, 0.3, -0.3])
    assert result.p_value > 0.5


def test_wilcoxon_drops_zero_differences() -> None:
    result = stats.wilcoxon_signed_rank([0.0, 0.0, 0.2, 0.3])
    assert result.n == 2


def test_wilcoxon_normal_branch_for_large_n() -> None:
    diffs = [0.01 * (i + 1) for i in range(40)]  # all positive, n>18
    result = stats.wilcoxon_signed_rank(diffs)
    assert result.method == "normal"
    assert result.p_value < 0.01


# --- bootstrap ------------------------------------------------------------- #


def test_bootstrap_is_deterministic_for_fixed_seed() -> None:
    values = [0.1, 0.2, 0.15, -0.05, 0.3, 0.0, 0.22]
    a = stats.bootstrap_mean_ci(values, seed=42, iterations=500)
    b = stats.bootstrap_mean_ci(values, seed=42, iterations=500)
    assert (a.estimate, a.low, a.high) == (b.estimate, b.low, b.high)


def test_bootstrap_brackets_point_estimate() -> None:
    values = [0.1, 0.2, 0.15, 0.18, 0.22, 0.12, 0.19]
    ci = stats.bootstrap_mean_ci(values, seed=1, iterations=1000)
    assert ci.low <= ci.estimate <= ci.high
    assert ci.estimate == pytest.approx(sum(values) / len(values))


def test_bootstrap_different_seeds_can_differ() -> None:
    values = [0.1, -0.3, 0.5, 0.0, 0.2, -0.1, 0.4, 0.05]
    a = stats.bootstrap_mean_ci(values, seed=1, iterations=300)
    b = stats.bootstrap_mean_ci(values, seed=2, iterations=300)
    # Same point estimate (mean of the data), but resampled intervals differ.
    assert a.estimate == pytest.approx(b.estimate)
    assert (a.low, a.high) != (b.low, b.high)
