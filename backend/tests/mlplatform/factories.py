"""Shared fixtures for the ML-data pipeline tests (zero infra, deterministic).

A tiny builder for :class:`RawTrace` rows and an :class:`InMemoryTraceSource`
seeded with a representative crew-trace corpus, so every unit test starts from
the same hermetic, reproducible data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.mlplatform.datasets.contracts import RawTrace
from app.mlplatform.datasets.sources import InMemoryTraceSource

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def raw(
    trace_id: str,
    prompt_key: str = "adapter@v3",
    *,
    prompt_version: str = "3.0.0",
    model: str = "qwen-plus",
    inputs: Mapping[str, Any] | None = None,
    output: str = '{"beats":[1]}',
    minutes: int = 0,
    book_id: str | None = "bk0",
    session_id: str | None = "s0",
    error: str | None = None,
    cache_hit: bool = False,
    qa: Mapping[str, Any] | None = None,
    director_edits: Sequence[Mapping[str, Any]] = (),
) -> RawTrace:
    return RawTrace(
        trace_id=trace_id,
        prompt_key=prompt_key,
        prompt_version=prompt_version,
        model=model,
        inputs=dict(inputs or {"page_text": f"beat {trace_id}"}),
        output=output,
        created_at=BASE + timedelta(minutes=minutes),
        book_id=book_id,
        session_id=session_id,
        error=error,
        cache_hit=cache_hit,
        qa=dict(qa) if qa else None,
        director_edits=tuple(dict(e) for e in director_edits),
    )


def corpus(n: int = 60, *, books: int = 10) -> InMemoryTraceSource:
    """A representative crew-trace corpus across roles / books / QA outcomes."""
    src = InMemoryTraceSource()
    for i in range(n):
        passed = i % 3 != 0
        src.add(
            raw(
                f"t{i}",
                prompt_key="adapter@v3" if i % 2 else "critic.qa",
                inputs={"page_text": f"Reach me at user{i}@mail.com beat {i}"},
                output='{"beats":[1]}' if i % 2 else '{"verdict":"pass"}',
                minutes=i,
                book_id=f"bk{i % books}",
                session_id=f"s{i % 5}",
                qa={
                    "verdict": "pass" if passed else "fail",
                    "score": 0.9 if passed else 0.2,
                    "ccs": 0.91 if passed else 0.4,
                },
                director_edits=[{"instruction": "make coat crimson"}] if i % 7 == 0 else (),
            )
        )
    return src
