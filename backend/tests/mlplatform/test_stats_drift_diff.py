"""Stats, drift detection, and structural diff between dataset versions."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import (
    AgentRole,
    Dataset,
    QAVerdict,
    Split,
    TaskType,
    TraceExample,
)
from app.mlplatform.datasets.diff import diff_datasets
from app.mlplatform.datasets.drift import (
    DriftSeverity,
    drift_between,
    js_divergence,
    ks_statistic,
    psi,
)
from app.mlplatform.datasets.stats import NumericSummary, compute_stats


def _ex(
    ex_id: str,
    *,
    role: AgentRole = AgentRole.ADAPTER,
    reward: float | None = None,
    passed: bool | None = None,
    output: str = "out",
    book: str = "bk0",
) -> TraceExample:
    qa = None if passed is None else QAVerdict(passed=passed, score=0.9 if passed else 0.1)
    return TraceExample(
        id=ex_id,
        role=role,
        task=TaskType.SFT,
        prompt_key="adapter@v3",
        prompt_version="3.0.0",
        model="qwen-plus",
        input={"page_text": ex_id},
        output=output,
        reward=reward,
        qa=qa,
        book_id=book,
    )


# -- stats ------------------------------------------------------------------ #


def test_numeric_summary_empty_safe() -> None:
    s = NumericSummary.of([])
    assert s.count == 0 and s.mean == 0.0
    s2 = NumericSummary.of([1.0, 2.0, 3.0, 4.0])
    assert s2.minimum == 1.0 and s2.maximum == 4.0 and s2.mean == 2.5


def test_compute_stats() -> None:
    ds = Dataset.from_examples(
        "d",
        [
            _ex("a", role=AgentRole.ADAPTER, passed=True, reward=0.9, book="bk0"),
            _ex("b", role=AgentRole.CRITIC, passed=False, reward=0.1, book="bk1"),
            _ex("c", role=AgentRole.ADAPTER, reward=0.5, book="bk0"),
        ],
    )
    st = compute_stats(ds)
    assert st.n == 3
    assert st.role_dist == {"adapter": 2, "critic": 1}
    assert st.book_count == 2
    assert st.qa_coverage == 2 / 3
    assert st.qa_pass_rate == 0.5
    assert st.role_entropy > 0


# -- drift metric primitives ------------------------------------------------ #


def test_psi_zero_when_identical() -> None:
    d = {"a": 50, "b": 50}
    assert psi(d, d) == 0.0


def test_psi_positive_on_shift() -> None:
    assert psi({"a": 90, "b": 10}, {"a": 10, "b": 90}) > 0.25


def test_js_divergence_bounds() -> None:
    assert js_divergence({"a": 1}, {"a": 1}) == 0.0
    assert 0.0 < js_divergence({"a": 1}, {"b": 1}) <= 1.0


def test_ks_statistic() -> None:
    assert ks_statistic([1, 2, 3], [1, 2, 3]) == 0.0
    assert ks_statistic([0, 0, 0], [10, 10, 10]) == 1.0


def test_drift_between_datasets() -> None:
    ref = Dataset.from_examples("ref", [_ex(f"r{i}", role=AgentRole.ADAPTER) for i in range(20)])
    # candidate shifts entirely to a different role → significant drift
    cand = Dataset.from_examples("cand", [_ex(f"c{i}", role=AgentRole.CRITIC) for i in range(20)])
    report = drift_between(ref, cand)
    assert report.has_significant_drift
    assert report.overall is DriftSeverity.SIGNIFICANT
    worst = report.worst()
    assert worst is not None


def test_no_drift_on_identical() -> None:
    ds = Dataset.from_examples("d", [_ex(f"e{i}") for i in range(10)])
    report = drift_between(ds, ds)
    assert report.overall in (DriftSeverity.NONE, DriftSeverity.MINOR)


# -- diff ------------------------------------------------------------------- #


def test_diff_added_removed_changed() -> None:
    base = Dataset.from_examples("d", [_ex("keep"), _ex("drop"), _ex("change", output="before")])
    target = Dataset.from_examples(
        "d", [_ex("keep"), _ex("change", output="after"), _ex("new")]
    )
    diff = diff_datasets(base, target)
    assert diff.added_ids == ("new",)
    assert diff.removed_ids == ("drop",)
    assert diff.changed_count == 1
    assert diff.changed[0].id == "change"
    assert "output" in diff.changed[0].changed_fields
    assert diff.unchanged_count == 1
    assert not diff.is_identical


def test_diff_detects_split_change() -> None:
    base = Dataset.from_examples("d", [_ex("a")])
    target = Dataset.from_examples("d", [_ex("a").with_split(Split.TEST)])
    diff = diff_datasets(base, target)
    # split is not in content_hash, but the diff still surfaces it (audit value)
    assert diff.changed_count == 1
    assert "split" in diff.changed[0].changed_fields


def test_diff_identical() -> None:
    ds = Dataset.from_examples("d", [_ex("a"), _ex("b")])
    assert diff_datasets(ds, ds).is_identical
