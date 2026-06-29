"""Filtering: composable predicates, reporting, curriculum, quality tiers."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import (
    AgentRole,
    Dataset,
    DirectorEdit,
    QAVerdict,
    TaskType,
    TraceExample,
)
from app.mlplatform.datasets.filtering import (
    QualityTier,
    all_of,
    any_of,
    apply_filter,
    difficulty,
    golden_subset,
    has_director_edit,
    min_reward,
    negate,
    non_empty,
    order_by_difficulty,
    qa_passed,
    quality_tiers,
    role_in,
    tier_of,
)


def _ex(
    ex_id: str,
    *,
    role: AgentRole = AgentRole.ADAPTER,
    reward: float | None = 0.5,
    passed: bool | None = None,
    output: str = "a valid output",
    edits: tuple[DirectorEdit, ...] = (),
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
        director_edits=edits,
        book_id="bk0",
    )


def test_predicates() -> None:
    assert qa_passed(_ex("a", passed=True))
    assert not qa_passed(_ex("b", passed=False))
    assert min_reward(0.7)(_ex("c", reward=0.8))
    assert not min_reward(0.7)(_ex("d", reward=0.5))
    assert role_in(AgentRole.ADAPTER)(_ex("e"))
    assert non_empty(4)(_ex("f", output="long enough"))
    assert not non_empty(4)(_ex("g", output="hi"))


def test_combinators() -> None:
    p = all_of(qa_passed, min_reward(0.5))
    assert p(_ex("a", passed=True, reward=0.9))
    assert not p(_ex("b", passed=True, reward=0.1))
    q = any_of(qa_passed, min_reward(0.99))
    assert q(_ex("c", passed=True, reward=0.0))
    assert negate(has_director_edit)(_ex("d"))


def test_apply_filter_report() -> None:
    ds = Dataset.from_examples(
        "d",
        [
            _ex("keep", passed=True, reward=0.9),
            _ex("drop_qa", passed=False, reward=0.9),
            _ex("drop_reward", passed=True, reward=0.1),
        ],
    )
    kept, report = apply_filter(
        ds,
        all_of(qa_passed, min_reward(0.5)),
        named={"qa": qa_passed, "reward": min_reward(0.5)},
    )
    assert len(kept) == 1
    assert kept.examples[0].id == "keep"
    assert report.dropped == 2
    assert report.by_named["qa"] == 1
    assert report.by_named["reward"] == 1
    assert 0 < report.keep_rate < 1


def test_curriculum_orders_easy_to_hard() -> None:
    easy = _ex("easy", passed=True, reward=0.95)
    hard = _ex("hard", passed=False, reward=0.1, edits=(DirectorEdit(instruction="fix"),))
    mid = _ex("mid", reward=0.5)
    ds = Dataset.from_examples("d", [hard, easy, mid])
    ordered = order_by_difficulty(ds, hardest_last=True)
    ids = [e.id for e in ordered.examples]
    assert ids.index("easy") < ids.index("mid") < ids.index("hard")
    assert difficulty(hard) > difficulty(easy)


def test_quality_tiers() -> None:
    gold = _ex("gold", passed=True, reward=0.9)
    silver = _ex("silver", passed=True, reward=0.6)
    bronze = _ex("bronze", passed=False, reward=0.1)
    ds = Dataset.from_examples("d", [gold, silver, bronze])
    tiers = quality_tiers(ds)
    assert tier_of(gold) is QualityTier.GOLD
    assert tier_of(silver) is QualityTier.SILVER
    assert tier_of(bronze) is QualityTier.BRONZE
    assert len(tiers[QualityTier.GOLD]) == 1


def test_golden_subset() -> None:
    ds = Dataset.from_examples(
        "d",
        [
            _ex("g", passed=True, reward=0.9),
            _ex("edited", passed=True, reward=0.9, edits=(DirectorEdit(instruction="x"),)),
            _ex("low", passed=True, reward=0.3),
        ],
    )
    golden, report = golden_subset(ds)
    assert {e.id for e in golden.examples} == {"g"}
    assert report.dropped == 2
