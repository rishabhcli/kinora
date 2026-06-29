"""Synthetic workload generator — deterministic fuel for every other layer's tests.

Generates realistic Kinora-shaped query streams so the profiler, N+1 detector,
index advisor, result cache, and matview rewriter can be exercised against a
*workload* rather than hand-written one-offs. Everything is driven by a seeded
:class:`random.Random`, so a given ``(seed, spec)`` always produces the identical
stream — the property the deterministic test suite depends on.

The generator knows the app's hot tables (``book``, ``shot``, ``entity``,
``continuity_state``, the §4.2 source-span index) and the queries the reading-room
loop issues against them: per-book shot seeks, source-span lookups, entity reads,
continuity reads, and a few aggregates. A **skew** parameter (Zipf-like) controls
how concentrated the parameter values are, so a generated stream can model the
"everyone hammers the same hot book" reality that creates cache wins and N+1
bursts.

Outputs:

* :meth:`WorkloadGenerator.stream` — an ordered list of :class:`GeneratedQuery`
  (sql + params + a logical latency), the raw event stream a profiler/detector
  consumes.
* :meth:`WorkloadGenerator.workload` — an aggregated
  :class:`~app.datascale.optimize.advisor.Workload` (shape → weight) the advisor
  consumes.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.datascale.optimize.advisor import Workload
from app.datascale.optimize.fingerprint import make_fingerprint


class QueryKind(StrEnum):
    """The query archetypes the generator emits."""

    SHOT_BY_BOOK = "shot_by_book"
    SHOT_BY_SOURCE_SPAN = "shot_by_source_span"
    SHOT_BY_ID = "shot_by_id"
    ENTITY_BY_BOOK = "entity_by_book"
    CONTINUITY_BY_SHOT = "continuity_by_shot"
    BOOK_BY_ID = "book_by_id"
    SHOT_COUNT_BY_BOOK = "shot_count_by_book"
    BOOK_LIST_RECENT = "book_list_recent"


#: The SQL template per kind. Parameters are bound positionally as ``$1`` etc.
_TEMPLATES: dict[QueryKind, str] = {
    QueryKind.SHOT_BY_BOOK: "SELECT id, beat_id, page_no FROM shot WHERE book_id = $1",
    QueryKind.SHOT_BY_SOURCE_SPAN: (
        "SELECT id FROM shot WHERE book_id = $1 AND span_start <= $2 AND span_end >= $2"
    ),
    QueryKind.SHOT_BY_ID: "SELECT id, beat_id, status FROM shot WHERE id = $1",
    QueryKind.ENTITY_BY_BOOK: "SELECT id, name, kind FROM entity WHERE book_id = $1",
    QueryKind.CONTINUITY_BY_SHOT: "SELECT state FROM continuity_state WHERE shot_id = $1",
    QueryKind.BOOK_BY_ID: "SELECT id, title, status FROM book WHERE id = $1",
    QueryKind.SHOT_COUNT_BY_BOOK: "SELECT book_id, count(*) FROM shot GROUP BY book_id",
    QueryKind.BOOK_LIST_RECENT: "SELECT id, title FROM book ORDER BY created_at DESC LIMIT 20",
}

#: Default relative frequency of each kind (the reading-room loop is shot-heavy).
_DEFAULT_MIX: dict[QueryKind, float] = {
    QueryKind.SHOT_BY_BOOK: 0.18,
    QueryKind.SHOT_BY_SOURCE_SPAN: 0.30,
    QueryKind.SHOT_BY_ID: 0.20,
    QueryKind.ENTITY_BY_BOOK: 0.10,
    QueryKind.CONTINUITY_BY_SHOT: 0.12,
    QueryKind.BOOK_BY_ID: 0.05,
    QueryKind.SHOT_COUNT_BY_BOOK: 0.03,
    QueryKind.BOOK_LIST_RECENT: 0.02,
}

#: A representative latency (ms) per kind, used as the recorded duration so the
#: profiler produces meaningful hot-path rankings.
_BASE_LATENCY_MS: dict[QueryKind, float] = {
    QueryKind.SHOT_BY_BOOK: 4.0,
    QueryKind.SHOT_BY_SOURCE_SPAN: 2.5,
    QueryKind.SHOT_BY_ID: 1.0,
    QueryKind.ENTITY_BY_BOOK: 3.0,
    QueryKind.CONTINUITY_BY_SHOT: 1.5,
    QueryKind.BOOK_BY_ID: 1.0,
    QueryKind.SHOT_COUNT_BY_BOOK: 25.0,
    QueryKind.BOOK_LIST_RECENT: 8.0,
}


@dataclass(frozen=True, slots=True)
class GeneratedQuery:
    """One emitted query event: SQL skeleton template + bound params + latency."""

    kind: QueryKind
    sql: str
    params: tuple[object, ...]
    latency_ms: float

    def fingerprint(self) -> str:
        """The shape fingerprint of this query (kind-stable across params)."""
        return make_fingerprint(self.sql).hexdigest


@dataclass(slots=True)
class WorkloadSpec:
    """Knobs controlling a generated workload."""

    n_queries: int = 1000
    n_books: int = 50
    n_shots_per_book: int = 200
    n_entities_per_book: int = 12
    #: Zipf-like skew exponent: 0 = uniform, higher = more concentrated on hot ids.
    skew: float = 1.0
    #: Latency jitter as a fraction of the base latency (±).
    latency_jitter: float = 0.25
    mix: dict[QueryKind, float] = field(default_factory=lambda: dict(_DEFAULT_MIX))


class WorkloadGenerator:
    """A seeded generator of Kinora-shaped query streams."""

    def __init__(self, *, seed: int = 1234, spec: WorkloadSpec | None = None) -> None:
        self._seed = seed
        self._spec = spec or WorkloadSpec()
        self._rng = random.Random(seed)
        self._book_weights = self._zipf_weights(self._spec.n_books, self._spec.skew)

    def reset(self) -> None:
        """Re-seed the RNG so the next stream reproduces the first."""
        self._rng = random.Random(self._seed)

    # ---- id sampling ---- #

    @staticmethod
    def _zipf_weights(n: int, skew: float) -> list[float]:
        """Normalised Zipf weights for ``n`` items (rank 1 hottest)."""
        if n <= 0:
            return []
        raw = [1.0 / ((rank + 1) ** skew) for rank in range(n)]
        total = sum(raw)
        return [w / total for w in raw]

    def _sample_book(self) -> int:
        idx = self._weighted_index(self._book_weights)
        return idx + 1  # book ids are 1-based

    def _weighted_index(self, weights: Sequence[float]) -> int:
        r = self._rng.random()
        cum = 0.0
        for i, w in enumerate(weights):
            cum += w
            if r <= cum:
                return i
        return len(weights) - 1

    def _sample_shot(self, book_id: int) -> int:
        # A shot id encodes its book for realistic FK locality.
        offset = self._rng.randrange(self._spec.n_shots_per_book)
        return book_id * 10_000 + offset

    def _sample_kind(self) -> QueryKind:
        kinds = list(self._spec.mix.keys())
        weights = list(self._spec.mix.values())
        total = sum(weights)
        norm = [w / total for w in weights]
        return kinds[self._weighted_index(norm)]

    # ---- query construction ---- #

    def _params_for(self, kind: QueryKind) -> tuple[object, ...]:
        if kind in (QueryKind.SHOT_BY_BOOK, QueryKind.ENTITY_BY_BOOK, QueryKind.BOOK_BY_ID):
            return (self._sample_book(),)
        if kind == QueryKind.SHOT_BY_SOURCE_SPAN:
            book = self._sample_book()
            span_pos = self._rng.randrange(self._spec.n_shots_per_book * 50)
            return (book, span_pos)
        if kind == QueryKind.SHOT_BY_ID:
            return (self._sample_shot(self._sample_book()),)
        if kind == QueryKind.CONTINUITY_BY_SHOT:
            return (self._sample_shot(self._sample_book()),)
        # Aggregates / list queries take no parameters.
        return ()

    def _latency_for(self, kind: QueryKind) -> float:
        base = _BASE_LATENCY_MS[kind]
        jitter = self._spec.latency_jitter
        factor = 1.0 + self._rng.uniform(-jitter, jitter)
        return round(max(0.1, base * factor), 3)

    def next_query(self) -> GeneratedQuery:
        """Generate one query event."""
        kind = self._sample_kind()
        params = self._params_for(kind)
        return GeneratedQuery(
            kind=kind,
            sql=_TEMPLATES[kind],
            params=params,
            latency_ms=self._latency_for(kind),
        )

    def stream(self, n: int | None = None) -> list[GeneratedQuery]:
        """Generate an ordered stream of ``n`` query events (default: spec count)."""
        count = n if n is not None else self._spec.n_queries
        return [self.next_query() for _ in range(count)]

    def workload(self, n: int | None = None) -> Workload:
        """Generate a stream and aggregate it into an advisor :class:`Workload`.

        Each distinct (kind) shape becomes one weighted query; the weight is the
        number of times it appeared, so the advisor sees the real hot/cold mix.
        """
        events = self.stream(n)
        counts: Counter[str] = Counter()
        sql_by_kind: dict[str, str] = {}
        for ev in events:
            counts[ev.kind] += 1
            sql_by_kind[ev.kind] = ev.sql
        wl = Workload()
        for kind in sorted(counts, key=lambda k: counts[k], reverse=True):
            wl.add(sql_by_kind[kind], weight=float(counts[kind]))
        return wl

    def n_plus_one_burst(self, kind: QueryKind, n: int) -> list[GeneratedQuery]:
        """Emit a deliberate N+1 burst: ``n`` calls of one shape, distinct params.

        Models the per-row loop (e.g. fetching each shot's continuity one at a
        time) the detector should flag and the dataloader should fix.
        """
        out: list[GeneratedQuery] = []
        for _ in range(n):
            out.append(
                GeneratedQuery(
                    kind=kind,
                    sql=_TEMPLATES[kind],
                    params=self._params_for(kind),
                    latency_ms=self._latency_for(kind),
                )
            )
        return out

    def table_sizes(self) -> dict[str, int]:
        """Realistic table-row estimates implied by the spec (for the advisor)."""
        books = self._spec.n_books
        shots = books * self._spec.n_shots_per_book
        entities = books * self._spec.n_entities_per_book
        return {
            "book": books,
            "shot": shots,
            "entity": entities,
            "continuity_state": shots,
        }


__all__ = [
    "GeneratedQuery",
    "QueryKind",
    "WorkloadGenerator",
    "WorkloadSpec",
]
