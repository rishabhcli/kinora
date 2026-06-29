"""Labeling: LF votes, the weighted-vote label model, coverage/conflict report."""

from __future__ import annotations

import pytest

from app.mlplatform.datasets.contracts import (
    AgentRole,
    DirectorEdit,
    QAVerdict,
    TaskType,
    TraceExample,
)
from app.mlplatform.datasets.errors import LabelError
from app.mlplatform.datasets.labeling import (
    ABSTAIN,
    BAD,
    GOOD,
    LF,
    LabelModel,
    apply_labeling,
    lf_director_edited,
    lf_empty_or_short,
    lf_qa_fail,
    lf_qa_pass,
    lf_valid_json,
)


def _ex(ex_id: str, **kw: object) -> TraceExample:
    defaults: dict[str, object] = {
        "id": ex_id,
        "role": AgentRole.ADAPTER,
        "task": TaskType.SFT,
        "prompt_key": "adapter@v3",
        "prompt_version": "3.0.0",
        "model": "qwen-plus",
        "input": {"page_text": "p"},
        "output": '{"beats":[1]}',
    }
    defaults.update(kw)
    return TraceExample(**defaults)  # type: ignore[arg-type]


def test_individual_lfs() -> None:
    assert lf_qa_pass(_ex("a", qa=QAVerdict(passed=True))) == GOOD
    assert lf_qa_pass(_ex("b", qa=QAVerdict(passed=False))) is ABSTAIN
    assert lf_qa_fail(_ex("c", qa=QAVerdict(passed=False))) == BAD
    assert lf_director_edited(_ex("d", director_edits=(DirectorEdit(instruction="x"),))) == BAD
    assert lf_empty_or_short(_ex("e", output="hi")) == BAD
    assert lf_valid_json(_ex("f", output="not json{")) == BAD
    assert lf_valid_json(_ex("g", output='{"ok":1}')) is ABSTAIN


def test_label_model_consensus() -> None:
    exs = [
        _ex("pass", qa=QAVerdict(passed=True, score=0.9), reward=0.9),
        _ex("fail", qa=QAVerdict(passed=False, score=0.1), reward=0.1, output="x"),
        _ex(
            "edited",
            director_edits=(DirectorEdit(instruction="fix"),),
            reward=0.2,
            output='{"beats":[]}',
        ),
    ]
    labeled, report = apply_labeling(exs)
    by_id = {e.id: e for e in labeled}
    assert by_id["pass"].labels["quality"] == GOOD
    assert by_id["fail"].labels["quality"] == BAD
    assert by_id["edited"].labels["quality"] == BAD
    assert report.coverage == 1.0
    assert "quality_conf" in by_id["pass"].weak_labels


def test_abstain_when_no_signal() -> None:
    labeled, report = apply_labeling([_ex("bare")])
    assert labeled[0].labels.get("quality") is None
    assert report.abstained == 1


def test_lf_stats_report() -> None:
    exs = [
        _ex(f"e{i}", qa=QAVerdict(passed=i % 2 == 0, score=0.9 if i % 2 == 0 else 0.1))
        for i in range(6)
    ]
    _, report = apply_labeling(exs)
    names = {s.name for s in report.lf_stats}
    assert "qa_pass" in names
    qa_pass_stat = next(s for s in report.lf_stats if s.name == "qa_pass")
    assert qa_pass_stat.coverage > 0


def test_duplicate_lf_names_rejected() -> None:
    with pytest.raises(LabelError):
        LabelModel(lfs=(LF("dup", lf_qa_pass), LF("dup", lf_qa_fail)))


def test_empty_input() -> None:
    labeled, report = apply_labeling([])
    assert labeled == []
    assert report.n == 0
