"""Ingest: role mapping, task inference, signal projection, drop policy, idempotency."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import AgentRole, TaskType
from app.mlplatform.datasets.ingest import (
    IngestConfig,
    derive_reward,
    infer_task,
    ingest_all,
    normalize,
    role_for_prompt_key,
)
from tests.mlplatform.factories import raw


def test_role_mapping() -> None:
    assert role_for_prompt_key("adapter@v3") is AgentRole.ADAPTER
    assert role_for_prompt_key("cinematographer.shot") is AgentRole.CINEMATOGRAPHER
    assert role_for_prompt_key("critic.qa") is AgentRole.CRITIC
    assert role_for_prompt_key("continuity@v1") is AgentRole.CONTINUITY
    assert role_for_prompt_key("showrunner.arb") is AgentRole.SHOWRUNNER
    assert role_for_prompt_key("totally_unknown") is AgentRole.UNKNOWN


def test_task_inference() -> None:
    from app.mlplatform.datasets.contracts import DirectorEdit, QAVerdict

    assert infer_task(None, ()) is TaskType.SFT
    assert infer_task(QAVerdict(passed=True), ()) is TaskType.PREFERENCE
    assert infer_task(None, (DirectorEdit(instruction="x"),)) is TaskType.PREFERENCE


def test_normalize_projects_qa_and_edits() -> None:
    r = raw(
        "t1",
        qa={"verdict": "pass", "score": 0.9, "ccs": 0.92, "learned_reward": 0.8},
        director_edits=[{"instruction": "make coat crimson", "region": "top"}],
    )
    ex = normalize(r)
    assert ex is not None
    assert ex.qa is not None and ex.qa.passed and ex.qa.ccs == 0.92
    assert len(ex.director_edits) == 1
    assert ex.director_edits[0].instruction == "make coat crimson"
    assert ex.task is TaskType.PREFERENCE


def test_drop_policy() -> None:
    assert normalize(raw("e", error="boom")) is None
    assert normalize(raw("c", cache_hit=True)) is None
    assert normalize(raw("empty", output="   ")) is None
    # disabling the drops keeps them
    cfg = IngestConfig(drop_errors=False, drop_cache_hits=False, drop_empty_output=False)
    assert normalize(raw("e2", error="boom"), config=cfg) is not None


def test_normalize_is_idempotent() -> None:
    r = raw("t1", qa={"verdict": "pass", "score": 0.9})
    assert normalize(r).id == normalize(r).id  # type: ignore[union-attr]


def test_derive_reward_penalizes_edits() -> None:
    from app.mlplatform.datasets.contracts import DirectorEdit, QAVerdict

    qa = QAVerdict(passed=True, score=1.0)
    no_edit = derive_reward(qa, ())
    one_edit = derive_reward(qa, (DirectorEdit(instruction="x"),))
    two_edit = derive_reward(qa, (DirectorEdit(instruction="x"), DirectorEdit(instruction="y")))
    assert no_edit is not None and one_edit is not None and two_edit is not None
    assert no_edit > one_edit > two_edit
    assert derive_reward(None, ()) is None


def test_ingest_all_stats() -> None:
    src = [
        raw("a", prompt_key="adapter@v3", qa={"verdict": "pass", "score": 0.9}),
        raw("b", prompt_key="critic.qa", qa={"verdict": "fail", "score": 0.1}),
        raw("err", error="x"),
        raw("cache", cache_hit=True),
    ]
    examples, stats = ingest_all(src)
    assert stats.seen == 4
    assert stats.kept == 2
    assert stats.dropped_error == 1
    assert stats.dropped_cache == 1
    assert stats.by_role == {"adapter": 1, "critic": 1}
    assert len(examples) == 2
