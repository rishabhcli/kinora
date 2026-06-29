"""Read-only trace sources — the seams the pipeline ingests through.

A :class:`~app.mlplatform.datasets.contracts.TraceSource` adapts an existing
observability plane into a stream of provider-agnostic
:class:`~app.mlplatform.datasets.contracts.RawTrace` rows **without importing or
mutating** that plane's write path. Two implementations ship here:

* :class:`LLMOpsTraceSource` — wraps an :class:`app.llmops.tracing.TraceStore`
  (the in-memory ring buffer or the DB-backed store, both behind the same
  ``query`` protocol) and projects each :class:`app.llmops.tracing.RunTrace` into
  a ``RawTrace``. It can be handed two read-only join callbacks — one that
  returns the Critic's QA record for a trace (§9.5) and one that returns the
  director edits for a trace's output (§5.4) — so the supervision signals come
  along, still without the dataset plane importing the agents.
* :class:`InMemoryTraceSource` — a plain list-backed fake used by the tests and
  by offline replays; zero infra, zero imports beyond the contracts.

The join callbacks are the *only* coupling to the Critic / director planes, and
they are read-only function seams supplied by the caller (the composition root),
so this module imports neither :mod:`app.agents` nor any write path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.mlplatform.datasets.contracts import RawTrace
from app.mlplatform.datasets.errors import SourceError


class _RunTraceLike(Protocol):
    """Structural type for an ``app.llmops.tracing.RunTrace`` (no hard import)."""

    id: str
    prompt_key: str
    prompt_version: str
    model: str
    inputs: Mapping[str, Any]
    output: str
    created_at: datetime
    book_id: str | None
    session_id: str | None
    error: str | None
    cache_hit: bool


class _TraceStoreLike(Protocol):
    """Structural type for an ``app.llmops.tracing.TraceStore``."""

    def query(self, q: Any) -> list[Any]: ...


#: Read-only join callbacks the caller supplies (resolved against the Critic /
#: director planes by the composition root). Both are pure lookups by trace id.
QAJoin = Callable[[str], Mapping[str, Any] | None]
EditsJoin = Callable[[str], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class LLMOpsTraceSource:
    """Project ``app.llmops`` run traces (+ optional QA / edits joins) to ``RawTrace``.

    ``query_factory`` builds the store's ``TraceQuery`` from ``(since, limit)``
    so this module need not import :class:`app.llmops.tracing.TraceQuery`; the
    composition root passes a tiny factory. When omitted, the store is queried
    with a permissive object the in-memory store understands (``None`` → all).
    """

    store: _TraceStoreLike
    qa_join: QAJoin | None = None
    edits_join: EditsJoin | None = None
    #: Builds the underlying ``TraceQuery``; defaults to a passthrough factory.
    query_factory: Callable[[datetime | None, int | None], Any] | None = None

    def _query(self, since: datetime | None, limit: int | None) -> Any:
        if self.query_factory is not None:
            return self.query_factory(since, limit)
        # Late, soft import so the fake/in-memory path needs no llmops at all.
        try:
            from app.llmops.tracing import TraceQuery
        except Exception as exc:  # pragma: no cover - defensive
            raise SourceError(
                "LLMOpsTraceSource needs a query_factory or app.llmops.tracing.TraceQuery"
            ) from exc
        return TraceQuery(since=since, limit=limit, newest_first=False)

    def _to_raw(self, t: _RunTraceLike) -> RawTrace:
        qa = self.qa_join(t.id) if self.qa_join is not None else None
        edits = self.edits_join(t.id) if self.edits_join is not None else ()
        return RawTrace(
            trace_id=t.id,
            prompt_key=t.prompt_key,
            prompt_version=t.prompt_version,
            model=t.model,
            inputs=dict(t.inputs),
            output=t.output,
            created_at=t.created_at,
            book_id=t.book_id,
            session_id=t.session_id,
            error=t.error,
            cache_hit=t.cache_hit,
            qa=dict(qa) if qa is not None else None,
            director_edits=tuple(dict(e) for e in edits),
        )

    def iter_raw(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> Iterable[RawTrace]:
        rows = self.store.query(self._query(since, limit))
        for row in rows:
            yield self._to_raw(row)

    def count(self, *, since: datetime | None = None) -> int:
        return len(list(self.store.query(self._query(since, None))))


@dataclass
class InMemoryTraceSource:
    """A list-backed fake :class:`TraceSource` for tests and offline replays."""

    rows: list[RawTrace] = field(default_factory=list)

    def add(self, raw: RawTrace) -> None:
        self.rows.append(raw)

    def extend(self, raws: Iterable[RawTrace]) -> None:
        self.rows.extend(raws)

    def _matching(self, since: datetime | None) -> list[RawTrace]:
        rows = sorted(self.rows, key=lambda r: r.created_at)
        if since is not None:
            rows = [r for r in rows if r.created_at >= since]
        return rows

    def iter_raw(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> Iterable[RawTrace]:
        rows = self._matching(since)
        if limit is not None:
            rows = rows[:limit]
        yield from rows

    def count(self, *, since: datetime | None = None) -> int:
        return len(self._matching(since))


__all__ = [
    "EditsJoin",
    "InMemoryTraceSource",
    "LLMOpsTraceSource",
    "QAJoin",
]
