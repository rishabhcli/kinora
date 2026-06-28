"""In-memory search backend — a full BM25 + vector + RRF engine, no infra.

This is a *real* search engine, not a stub: an inverted index with positional
postings (for phrase matching), per-field BM25, a dense cosine arm, reciprocal-
rank fusion, facet aggregation, fuzzy term expansion (edit distance) and prefix
suggestions. It backs the unit tests (deterministic, offline, zero credits) and
any zero-infrastructure deployment, and it is the reference implementation the
Postgres backend's behaviour is validated against.

Everything is pure Python + the project's analyzer/ranking/highlight modules.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.memory.retrieval import cosine
from app.search.analyzer import Analyzer, auto_fuzziness, default_analyzer, within_distance
from app.search.documents import FIELD_BOOSTS, DocKind, SearchDocument
from app.search.highlight import highlight
from app.search.index import (
    Facet,
    FacetCount,
    FieldHighlight,
    SearchHit,
    SearchMode,
    SearchRequest,
    SearchResponse,
)
from app.search.query import (
    Occur,
    ParsedQuery,
    PhraseClause,
    TermClause,
    range_matches,
)
from app.search.ranking import BM25, RankedList, reciprocal_rank_fusion

_TEXT_FIELDS = ("title", "body", "keywords")

#: A field-scoped clause aliasing: ``name:`` searches the ``title`` field.
_FIELD_ALIAS = {"name": "title"}


def _field_in_scope(clause_field: str | None, index_field: str) -> bool:
    """True when a clause's field scope permits matching ``index_field``.

    An unscoped clause matches every text field; a scoped clause matches only its
    field (with ``name`` aliased to ``title``).
    """
    if clause_field is None:
        return True
    return _FIELD_ALIAS.get(clause_field, clause_field) == index_field


@dataclass
class _Posting:
    """A term's occurrence in one field of one document: count + positions."""

    tf: int = 0
    positions: list[int] = field(default_factory=list)


@dataclass
class _IndexedDoc:
    """The internal record for one stored document + its analyzed term stats."""

    document: SearchDocument
    # field -> term -> posting
    postings: dict[str, dict[str, _Posting]]
    # field -> analyzed length (token count, stopwords excluded)
    field_len: dict[str, int]


class InMemoryIndex:
    """A complete in-memory hybrid search index (satisfies ``SearchIndex``)."""

    def __init__(self, *, analyzer: Analyzer | None = None) -> None:
        self._analyzer = analyzer or default_analyzer()
        self._docs: dict[str, _IndexedDoc] = {}
        # field -> term -> set(doc_id)  (the inverted index, for IDF + lookup)
        self._inverted: dict[str, dict[str, set[str]]] = {
            f: defaultdict(set) for f in _TEXT_FIELDS
        }
        # field -> running total token length (for avg length)
        self._total_len: dict[str, int] = dict.fromkeys(_TEXT_FIELDS, 0)
        self._vocab: set[str] = set()

    # -- mutation ----------------------------------------------------------- #

    async def upsert(self, documents: Iterable[SearchDocument]) -> int:
        count = 0
        for doc in documents:
            self._remove(doc.doc_id)
            self._add(doc)
            count += 1
        return count

    async def delete(self, doc_ids: Iterable[str]) -> int:
        return sum(1 for d in doc_ids if self._remove(d))

    async def delete_by_book(self, book_id: str) -> int:
        targets = [d for d, rec in self._docs.items() if rec.document.book_id == book_id]
        for doc_id in targets:
            self._remove(doc_id)
        return len(targets)

    async def clear(self) -> None:
        self._docs.clear()
        self._inverted = {f: defaultdict(set) for f in _TEXT_FIELDS}
        self._total_len = dict.fromkeys(_TEXT_FIELDS, 0)
        self._vocab.clear()

    async def count(self, *, book_id: str | None = None) -> int:
        if book_id is None:
            return len(self._docs)
        return sum(1 for rec in self._docs.values() if rec.document.book_id == book_id)

    def _add(self, doc: SearchDocument) -> None:
        postings: dict[str, dict[str, _Posting]] = {}
        field_len: dict[str, int] = {}
        for fname, text in doc.text_fields().items():
            terms = self._analyzer.analyze_positions(text)
            fp: dict[str, _Posting] = {}
            length = 0
            for at in terms:
                if self._analyzer.is_stop(at.surface):
                    continue
                length += 1
                p = fp.setdefault(at.term, _Posting())
                p.tf += 1
                p.positions.append(at.position)
                self._inverted[fname][at.term].add(doc.doc_id)
                self._vocab.add(at.term)
            postings[fname] = fp
            field_len[fname] = length
            self._total_len[fname] += length
        self._docs[doc.doc_id] = _IndexedDoc(
            document=doc, postings=postings, field_len=field_len
        )

    def _remove(self, doc_id: str) -> bool:
        rec = self._docs.pop(doc_id, None)
        if rec is None:
            return False
        for fname, fp in rec.postings.items():
            self._total_len[fname] -= rec.field_len.get(fname, 0)
            for term in fp:
                bucket = self._inverted[fname].get(term)
                if bucket is not None:
                    bucket.discard(doc_id)
                    if not bucket:
                        del self._inverted[fname][term]
        return True

    # -- search ------------------------------------------------------------- #

    async def search(self, request: SearchRequest) -> SearchResponse:
        start = time.perf_counter()
        candidates = self._scope(request)

        lexical = self._lexical_rank(request.query, candidates)
        semantic = self._semantic_rank(request.query_embedding, candidates)

        fused = self._fuse(request, lexical, semantic)
        # Apply boolean MUST / MUST_NOT post-filters (lexical correctness).
        filtered = [d for d in fused if self._boolean_ok(request.query, d)]

        total = len(filtered)
        page = filtered[request.offset : request.offset + request.limit]
        lex_pos = {d: i for i, d in enumerate(lexical)}
        sem_pos = {d: i for i, d in enumerate(semantic)}
        fused_scores = dict(self._fused_scores(request, lexical, semantic))

        hits = [
            self._make_hit(
                doc_id,
                score=fused_scores.get(doc_id, 0.0),
                request=request,
                lexical_rank=lex_pos.get(doc_id),
                semantic_rank=sem_pos.get(doc_id),
            )
            for doc_id in page
        ]
        facets = self._facets(request, filtered)
        took = (time.perf_counter() - start) * 1000.0
        return SearchResponse(
            hits=hits, total=total, facets=facets, took_ms=round(took, 3), mode=request.mode
        )

    def _scope(self, request: SearchRequest) -> list[str]:
        """The candidate doc-id set after hard scope + filter/range constraints."""
        out: list[str] = []
        kinds = set(request.kinds) if request.kinds else None
        for doc_id, rec in self._docs.items():
            doc = rec.document
            if request.book_id is not None and doc.book_id != request.book_id:
                continue
            if kinds is not None and doc.kind not in kinds:
                continue
            if not self._passes_filters(request.query, doc):
                continue
            if not self._passes_ranges(request.query, doc):
                continue
            out.append(doc_id)
        return out

    def _passes_filters(self, query: ParsedQuery, doc: SearchDocument) -> bool:
        for f in query.filters:
            actual = doc.facet_value(f.field)
            match = actual is not None and actual.lower() == f.value.lower()
            if f.negate and match:
                return False
            if not f.negate and not match:
                return False
        return True

    def _passes_ranges(self, query: ParsedQuery, doc: SearchDocument) -> bool:
        return all(range_matches(r, doc.number(r.field)) for r in query.ranges)

    # -- lexical arm (BM25 over the inverted index) ------------------------- #

    def _lexical_rank(self, query: ParsedQuery, candidates: Sequence[str]) -> list[str]:
        if not query.has_text or not candidates:
            return list(candidates) if not query.has_text else []
        cand_set = set(candidates)
        scores = self._bm25_scores(query, cand_set)
        # Phrase clauses add a positional bonus (and gate MUST phrases).
        for phrase in query.phrases:
            self._apply_phrase(phrase, cand_set, scores)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [d for d, s in ranked if s > 0.0]

    def _bm25_scores(self, query: ParsedQuery, cand_set: set[str]) -> dict[str, float]:
        scores: dict[str, float] = defaultdict(float)
        for fname in _TEXT_FIELDS:
            num_docs = len(self._docs)
            avg_len = self._total_len[fname] / num_docs if num_docs else 1.0
            bm25 = BM25(num_docs=num_docs, avg_doc_len=avg_len)
            boost = FIELD_BOOSTS.get(fname, 1.0)
            for clause in query.terms:
                if clause.occur is Occur.MUST_NOT:
                    continue
                if not _field_in_scope(clause.field, fname):
                    continue
                for term in self._expand_term(clause, fname):
                    matched = self._inverted[fname].get(term)
                    if not matched:
                        continue
                    df = len(matched)
                    for doc_id in matched & cand_set:
                        rec = self._docs[doc_id]
                        posting = rec.postings[fname].get(term)
                        if posting is None:
                            continue
                        s = bm25.score_term(
                            tf=posting.tf, doc_len=rec.field_len[fname], doc_freq=df
                        )
                        scores[doc_id] += boost * s
        return scores

    def _expand_term(self, clause: TermClause, fname: str) -> list[str]:
        """The analyzed query term plus its fuzzy variants present in the field vocab."""
        analyzed = self._analyzer.analyze(clause.text)
        out: list[str] = list(analyzed)
        if not clause.fuzzy:
            return out
        field_vocab = self._inverted[fname].keys()
        for base in analyzed:
            budget = auto_fuzziness(base)
            if budget == 0:
                continue
            for vocab_term in field_vocab:
                if vocab_term == base:
                    continue
                if abs(len(vocab_term) - len(base)) > budget:
                    continue
                if within_distance(base, vocab_term, budget):
                    out.append(vocab_term)
        return out

    def _apply_phrase(
        self, phrase: PhraseClause, cand_set: set[str], scores: dict[str, float]
    ) -> None:
        terms = self._analyzer.analyze_phrase(phrase.text)
        if not terms:
            return
        fields = (phrase.field,) if phrase.field else _TEXT_FIELDS
        matched_docs: set[str] = set()
        for doc_id in cand_set:
            rec = self._docs[doc_id]
            hit = False
            for fname in fields:
                if fname not in rec.postings:
                    continue
                if self._phrase_in_field(terms, rec.postings[fname], slop=phrase.slop):
                    scores[doc_id] = scores.get(doc_id, 0.0) + 5.0  # phrase boost
                    hit = True
            if hit:
                matched_docs.add(doc_id)
        if phrase.occur is Occur.MUST:
            # Drop any candidate that didn't satisfy a required phrase.
            for doc_id in list(scores):
                if doc_id not in matched_docs:
                    scores[doc_id] = -1.0

    @staticmethod
    def _phrase_in_field(
        terms: Sequence[str], field_postings: dict[str, _Posting], *, slop: int
    ) -> bool:
        """True when the analyzed phrase terms appear adjacent (within ``slop``)."""
        first = field_postings.get(terms[0])
        if first is None:
            return False
        for start in first.positions:
            if InMemoryIndex._match_from(terms, field_postings, start, slop):
                return True
        return False

    @staticmethod
    def _match_from(
        terms: Sequence[str], field_postings: dict[str, _Posting], start: int, slop: int
    ) -> bool:
        expected = start
        for term in terms:
            posting = field_postings.get(term)
            if posting is None:
                return False
            ok = any(expected <= p <= expected + slop for p in posting.positions)
            if not ok:
                return False
            # Advance expected to the matched position + 1.
            nxt = min((p for p in posting.positions if p >= expected), default=None)
            if nxt is None:
                return False
            expected = nxt + 1
        return True

    # -- semantic arm (cosine over the dense vectors) ----------------------- #

    def _semantic_rank(
        self, query_vec: Sequence[float] | None, candidates: Sequence[str]
    ) -> list[str]:
        if query_vec is None:
            return []
        scored: list[tuple[str, float]] = []
        for doc_id in candidates:
            emb = self._docs[doc_id].document.embedding
            if emb is None:
                continue
            scored.append((doc_id, cosine(query_vec, emb)))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [d for d, s in scored if s > 0.0]

    # -- fusion ------------------------------------------------------------- #

    def _fuse(
        self, request: SearchRequest, lexical: list[str], semantic: list[str]
    ) -> list[str]:
        return [d for d, _ in self._fused_scores(request, lexical, semantic)]

    def _fused_scores(
        self, request: SearchRequest, lexical: list[str], semantic: list[str]
    ) -> list[tuple[str, float]]:
        if request.mode is SearchMode.LEXICAL:
            return [(d, 1.0 / (i + 1)) for i, d in enumerate(lexical)]
        if request.mode is SearchMode.SEMANTIC:
            return [(d, 1.0 / (i + 1)) for i, d in enumerate(semantic)]
        lists = [
            RankedList(doc_ids=lexical, weight=request.lexical_weight),
            RankedList(doc_ids=semantic, weight=request.semantic_weight),
        ]
        return reciprocal_rank_fusion(lists, k=request.rrf_k)

    def _boolean_ok(self, query: ParsedQuery, doc_id: str) -> bool:
        """Enforce MUST / MUST_NOT term clauses against the candidate document."""
        rec = self._docs.get(doc_id)
        if rec is None:
            return False
        for clause in query.terms:
            present = self._term_present(clause, rec)
            if clause.occur is Occur.MUST and not present:
                return False
            if clause.occur is Occur.MUST_NOT and present:
                return False
        return True

    def _term_present(self, clause: TermClause, rec: _IndexedDoc) -> bool:
        fields = (clause.field,) if clause.field else _TEXT_FIELDS
        for term in self._analyzer.analyze(clause.text):
            for fname in fields:
                fp = rec.postings.get(fname)
                if fp and term in fp:
                    return True
        return False

    # -- facets ------------------------------------------------------------- #

    def _facets(self, request: SearchRequest, doc_ids: Sequence[str]) -> list[Facet]:
        facets: list[Facet] = []
        for fname in request.facet_fields:
            counts: dict[str, int] = defaultdict(int)
            for doc_id in doc_ids:
                value = self._docs[doc_id].document.facet_value(fname)
                if value is not None:
                    counts[value] += 1
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            facets.append(
                Facet(field=fname, counts=[FacetCount(value=v, count=c) for v, c in ordered])
            )
        return facets

    # -- hit assembly + highlighting ---------------------------------------- #

    def _make_hit(
        self,
        doc_id: str,
        *,
        score: float,
        request: SearchRequest,
        lexical_rank: int | None,
        semantic_rank: int | None,
    ) -> SearchHit:
        doc = self._docs[doc_id].document
        highlights: list[FieldHighlight] = []
        if request.highlight and request.query.has_text:
            terms = set(self._analyzer.analyze(request.query.free_text))
            for fname, text in (("title", doc.title), ("body", doc.body)):
                if not text:
                    continue
                snippet = highlight(text, terms, analyzer=self._analyzer)
                if "<mark>" in snippet.text or fname == "title":
                    highlights.append(FieldHighlight(field=fname, snippet=snippet.text))
        return SearchHit(
            doc_id=doc.doc_id,
            kind=doc.kind,
            ref_id=doc.ref_id,
            book_id=doc.book_id,
            score=round(score, 6),
            title=doc.title,
            highlights=highlights,
            lexical_rank=lexical_rank,
            semantic_rank=semantic_rank,
            payload=dict(doc.payload),
        )

    # -- suggestions -------------------------------------------------------- #

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[str]:
        if not prefix:
            return []
        analyzed = self._analyzer.analyze(prefix)
        base = analyzed[0] if analyzed else prefix.lower()
        raw = prefix.lower().strip().split()[-1] if prefix.strip() else ""
        prefix_hits = sorted(
            (t for t in self._vocab if t.startswith(base) or t.startswith(raw)),
            key=lambda t: (len(t), t),
        )
        if len(prefix_hits) >= limit:
            return prefix_hits[:limit]
        # Backfill with fuzzy matches for the typo case.
        budget = auto_fuzziness(base)
        fuzzy = (
            sorted(
                (
                    t
                    for t in self._vocab
                    if t not in prefix_hits and within_distance(base, t, budget)
                ),
                key=lambda t: (len(t), t),
            )
            if budget
            else []
        )
        return (prefix_hits + fuzzy)[:limit]

    def known_kinds(self) -> set[DocKind]:
        """The doc kinds currently present (used by the service's facet defaults)."""
        return {rec.document.kind for rec in self._docs.values()}


__all__ = ["InMemoryIndex"]
