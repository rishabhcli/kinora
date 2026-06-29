"""Trace sources: the in-memory fake + the read-only llmops adapter (with fakes)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.mlplatform.datasets.sources import InMemoryTraceSource, LLMOpsTraceSource
from tests.mlplatform.factories import BASE, raw


def test_in_memory_source_iter_and_count() -> None:
    src = InMemoryTraceSource()
    src.extend([raw("a", minutes=0), raw("b", minutes=5), raw("c", minutes=10)])
    assert src.count() == 3
    assert [r.trace_id for r in src.iter_raw()] == ["a", "b", "c"]  # sorted by time
    assert [r.trace_id for r in src.iter_raw(limit=2)] == ["a", "b"]
    since = BASE.replace(minute=5)
    assert [r.trace_id for r in src.iter_raw(since=since)] == ["b", "c"]


# -- a minimal fake RunTrace + store mirroring the llmops shapes ------------ #


@dataclass(frozen=True)
class _FakeRunTrace:
    id: str
    prompt_key: str
    prompt_version: str
    model: str
    inputs: dict[str, Any]
    output: str
    created_at: datetime
    book_id: str | None = None
    session_id: str | None = None
    error: str | None = None
    cache_hit: bool = False


@dataclass
class _FakeStore:
    rows: list[_FakeRunTrace] = field(default_factory=list)

    def query(self, q: Any) -> list[_FakeRunTrace]:  # q ignored by the fake
        return list(self.rows)


def test_llmops_source_projects_runtrace_with_joins() -> None:
    store = _FakeStore(
        rows=[
            _FakeRunTrace(
                id="t1",
                prompt_key="adapter@v3",
                prompt_version="3.0.0",
                model="qwen-plus",
                inputs={"page_text": "p"},
                output='{"beats":[]}',
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                book_id="bk0",
                session_id="s0",
            )
        ]
    )
    qa_calls: list[str] = []

    def qa_join(trace_id: str) -> dict[str, Any] | None:
        qa_calls.append(trace_id)
        return {"verdict": "pass", "score": 0.9}

    def edits_join(trace_id: str) -> list[dict[str, Any]]:
        return [{"instruction": "make coat crimson"}]

    src = LLMOpsTraceSource(
        store=store,
        qa_join=qa_join,
        edits_join=edits_join,
        query_factory=lambda since, limit: None,
    )
    rows = list(src.iter_raw())
    assert len(rows) == 1
    r = rows[0]
    assert r.trace_id == "t1"
    assert r.qa == {"verdict": "pass", "score": 0.9}
    assert r.director_edits[0]["instruction"] == "make coat crimson"
    assert qa_calls == ["t1"]
    assert src.count() == 1


def test_llmops_source_without_joins() -> None:
    store = _FakeStore(
        rows=[
            _FakeRunTrace(
                id="t2",
                prompt_key="critic.qa",
                prompt_version="1.0.0",
                model="qwen-vl",
                inputs={"x": 1},
                output="{}",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
    )
    src = LLMOpsTraceSource(store=store, query_factory=lambda since, limit: None)
    r = next(iter(src.iter_raw()))
    assert r.qa is None
    assert r.director_edits == ()
