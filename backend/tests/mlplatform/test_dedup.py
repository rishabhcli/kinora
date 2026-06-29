"""Dedup: exact collapse, near-duplicate LSH, best-representative survival."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import (
    AgentRole,
    DirectorEdit,
    QAVerdict,
    TaskType,
    TraceExample,
)
from app.mlplatform.datasets.dedup import (
    NearDedupConfig,
    dedup,
    dedup_exact,
    dedup_near,
    default_scorer,
)


def _ex(ex_id: str, output: str = "the cat sat on the mat", **kw: object) -> TraceExample:
    defaults: dict[str, object] = {
        "id": ex_id,
        "role": AgentRole.ADAPTER,
        "task": TaskType.SFT,
        "prompt_key": "adapter@v3",
        "prompt_version": "3.0.0",
        "model": "qwen-plus",
        "input": {"page_text": "p"},
        "output": output,
    }
    defaults.update(kw)
    return TraceExample(**defaults)  # type: ignore[arg-type]


def test_exact_dedup_collapses_identical_content() -> None:
    a = _ex("a", book_id="bk1")
    b = _ex("b", book_id="bk2")  # different provenance, same content
    kept, report = dedup_exact([a, b])
    assert len(kept) == 1
    assert report.exact_removed == 1


def test_exact_dedup_keeps_best_representative() -> None:
    from datetime import UTC, datetime

    # Identical *content* (same hash) but different created_at → the scorer's
    # newest-tiebreak decides the survivor.
    older = _ex("older", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _ex("newer", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    assert older.content_hash == newer.content_hash
    kept, report = dedup_exact([older, newer])
    assert len(kept) == 1
    assert report.exact_removed == 1
    assert kept[0].id == "newer"  # newer wins the tiebreak


def test_scorer_ranks_signals() -> None:
    plain = _ex("plain")
    rich = _ex("rich", qa=QAVerdict(passed=True, score=0.95))
    assert default_scorer(rich) > default_scorer(plain)


def test_near_dedup_catches_whitespace_variants() -> None:
    a = _ex("a", output="the quick brown fox jumps over the lazy dog")
    b = _ex("b", output="the  quick brown fox  jumps over the lazy dog")  # extra spaces
    kept, report = dedup_near([a, b])
    assert len(kept) == 1
    assert report.near_removed == 1


def test_near_dedup_keeps_distinct_examples() -> None:
    a = _ex("a", output="a story about a knight and a dragon at dawn")
    b = _ex("b", output="a recipe for sourdough bread with rye flour")
    kept, _ = dedup_near([a, b])
    assert len(kept) == 2


def test_near_dedup_survivor_is_best() -> None:
    a = _ex("a", output="the quick brown fox jumps", reward=0.2)
    b = _ex("b", output="the quick brown fox jumps!", reward=0.9)
    kept, _ = dedup_near([a, b], config=NearDedupConfig(threshold=0.7))
    assert len(kept) == 1
    assert kept[0].id == "b"


def test_director_edit_beats_plain_in_scorer() -> None:
    plain = _ex("p")
    edited = _ex("e", director_edits=(DirectorEdit(instruction="fix"),))
    assert default_scorer(edited) > default_scorer(plain)


def test_top_level_dedup_toggle() -> None:
    a = _ex("a", output="x y z")
    b = _ex("b", output="x y z")  # exact dup
    near_only = _ex("c", output="x y z ")  # near dup
    kept_exact, _ = dedup([a, b, near_only], near=False)
    assert len(kept_exact) == 2  # near dup survives exact-only
    kept_near, _ = dedup([a, b, near_only], near=True)
    assert len(kept_near) == 1
