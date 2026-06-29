"""Metadata filtering + keyword fusion for hybrid search.

Two orthogonal signals are combined with the dense vector score:

* **Metadata predicates** — a small, JSON-friendly query language (``$eq``,
  ``$ne``, ``$in``, ``$nin``, ``$gt``/``$gte``/``$lt``/``$lte``, ``$exists``,
  ``$contains``, ``$and``/``$or``/``$not``). A plain ``{field: value}`` mapping
  is sugar for equality. These drive **pre-filtering** (restrict the candidate
  set before the ANN walk) and **post-filtering** (drop non-matching ANN hits).
* **Keyword fusion** — a lightweight BM25-style lexical scorer over a token
  field in the metadata, fused with the dense score so an exact term the
  embedding glossed over can still surface (the §8.4 hybrid-retrieval idea,
  applied at the ANN layer rather than the canon re-rank layer).

Everything here is pure and deterministic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .types import Metadata, VectorId

_TOKEN = re.compile(r"[a-z0-9]+")

_COMPARATORS = {"$gt", "$gte", "$lt", "$lte"}


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (the lexical unit for keyword fusion)."""
    return _TOKEN.findall(text.lower())


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _match_op(op: str, expected: Any, actual: Any) -> bool:  # noqa: C901 - small dispatch
    if op == "$eq":
        return actual == expected
    if op == "$ne":
        return actual != expected
    if op == "$in":
        return isinstance(expected, (list, tuple, set)) and actual in expected
    if op == "$nin":
        return isinstance(expected, (list, tuple, set)) and actual not in expected
    if op == "$exists":
        present = actual is not _MISSING
        return present is bool(expected)
    if op == "$contains":
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        if isinstance(actual, str):
            return isinstance(expected, str) and expected in actual
        return False
    if op in _COMPARATORS:
        a = _coerce_number(actual)
        e = _coerce_number(expected)
        if a is None or e is None:
            return False
        if op == "$gt":
            return a > e
        if op == "$gte":
            return a >= e
        if op == "$lt":
            return a < e
        return a <= e
    raise ValueError(f"unknown filter operator: {op}")


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


_MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class Predicate:
    """A compiled metadata filter. Build via :meth:`coerce` from a mapping."""

    spec: Mapping[str, Any]

    @staticmethod
    def coerce(
        where: Predicate | Mapping[str, Any] | None,
    ) -> Predicate | None:
        """Normalise the various accepted shapes into a :class:`Predicate`."""
        if where is None:
            return None
        if isinstance(where, Predicate):
            return where
        if isinstance(where, Mapping):
            return Predicate(dict(where))
        raise TypeError(f"unsupported filter type: {type(where)!r}")

    def matches(self, metadata: Metadata | None) -> bool:
        """Evaluate the predicate against a metadata mapping (``None`` = empty)."""
        return _eval(self.spec, metadata or {})


def _eval(spec: Mapping[str, Any], meta: Mapping[str, Any]) -> bool:  # noqa: C901
    for key, cond in spec.items():
        if key == "$and":
            if not all(_eval(c, meta) for c in cond):
                return False
        elif key == "$or":
            if not any(_eval(c, meta) for c in cond):
                return False
        elif key == "$not":
            if _eval(cond, meta):
                return False
        else:
            actual = meta.get(key, _MISSING)
            if isinstance(cond, Mapping) and any(str(k).startswith("$") for k in cond):
                for op, expected in cond.items():
                    if op == "$exists":
                        if not _match_op(op, expected, actual):
                            return False
                    elif actual is _MISSING or not _match_op(op, expected, actual):
                        return False
            else:  # bare value → equality
                if actual is _MISSING or actual != cond:
                    return False
    return True


@dataclass(slots=True)
class Bm25KeywordIndex:
    """A tiny BM25 lexical index over a token field for keyword fusion.

    Documents are added by id with their tokens; :meth:`score` returns a mapping
    of ``id -> bm25`` for a query's terms. Scores are min-max normalised to
    ``[0, 1]`` by :meth:`score_normalised` so they fuse cleanly with the dense
    cosine score (also mapped to ``[0, 1]`` upstream).
    """

    k1: float = 1.5
    b: float = 0.75
    _df: dict[str, int] = None  # type: ignore[assignment]
    _postings: dict[str, dict[VectorId, int]] = None  # type: ignore[assignment]
    _len: dict[VectorId, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._df = {}
        self._postings = {}
        self._len = {}

    @property
    def n_docs(self) -> int:
        return len(self._len)

    @property
    def avg_len(self) -> float:
        return (sum(self._len.values()) / self.n_docs) if self._len else 0.0

    def add(self, vid: VectorId, tokens: Sequence[str]) -> None:
        """Index ``vid``'s tokens (replacing any prior posting for it)."""
        if vid in self._len:
            self.remove(vid)
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        for term, c in counts.items():
            self._postings.setdefault(term, {})[vid] = c
            self._df[term] = self._df.get(term, 0) + 1
        self._len[vid] = len(tokens)

    def add_text(self, vid: VectorId, text: str) -> None:
        self.add(vid, tokenize(text))

    def remove(self, vid: VectorId) -> bool:
        if vid not in self._len:
            return False
        for term, posting in list(self._postings.items()):
            if vid in posting:
                del posting[vid]
                self._df[term] -= 1
                if self._df[term] <= 0:
                    del self._df[term]
                if not posting:
                    del self._postings[term]
        del self._len[vid]
        return True

    def _idf(self, term: str) -> float:
        n = self.n_docs
        df = self._df.get(term, 0)
        # BM25+ idf floor keeps it non-negative for very common terms.
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Sequence[str]) -> dict[VectorId, float]:
        """Raw BM25 score per candidate doc for the query terms."""
        if not self._len:
            return {}
        avg = self.avg_len or 1.0
        scores: dict[VectorId, float] = {}
        for term in set(query_tokens):
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf(term)
            for vid, tf in posting.items():
                dl = self._len[vid]
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avg)
                scores[vid] = scores.get(vid, 0.0) + idf * (tf * (self.k1 + 1.0)) / denom
        return scores

    def score_normalised(self, query_tokens: Sequence[str]) -> dict[VectorId, float]:
        """BM25 scores min-max scaled into ``[0, 1]`` for fusion."""
        raw = self.score(query_tokens)
        if not raw:
            return {}
        lo = min(raw.values())
        hi = max(raw.values())
        if hi <= lo:
            return dict.fromkeys(raw, 1.0)
        span = hi - lo
        return {vid: (s - lo) / span for vid, s in raw.items()}


def cosine_to_unit(score: float) -> float:
    """Map a cosine similarity in ``[-1, 1]`` to ``[0, 1]`` for fusion."""
    return max(0.0, min(1.0, (score + 1.0) / 2.0))


def fuse_scores(
    dense: Mapping[VectorId, float],
    lexical: Mapping[VectorId, float],
    *,
    alpha: float,
) -> dict[VectorId, float]:
    """Linear fusion ``alpha·dense + (1-alpha)·lexical`` over the id union.

    Missing entries are treated as 0 — a doc that only the lexical index found
    still ranks, and vice-versa, which is the point of hybrid recall.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha must be in [0, 1]")
    out: dict[VectorId, float] = {}
    for vid, s in dense.items():
        out[vid] = alpha * s
    for vid, s in lexical.items():
        out[vid] = out.get(vid, 0.0) + (1.0 - alpha) * s
    return out


__all__ = [
    "Bm25KeywordIndex",
    "Predicate",
    "cosine_to_unit",
    "fuse_scores",
    "tokenize",
]
