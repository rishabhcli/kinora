"""Postgres search backend — FTS (tsvector) + pgvector hybrid, fused by RRF.

The production :class:`~app.search.index.SearchIndex` over the ``search_documents``
table (kinora.md §8 projection):

* the **lexical arm** runs ``websearch_to_tsquery`` against the generated
  weighted ``search_vector`` column, ranked by ``ts_rank_cd`` (title>kw>body via
  the A/B/C weights), using the GIN index;
* the **dense arm** runs a pgvector cosine nearest-neighbour (``<=>``) using the
  HNSW index;
* the two ranked lists are fused with reciprocal-rank fusion in Python
  (:func:`app.search.ranking.reciprocal_rank_fusion`) so the hybrid behaviour is
  identical to the in-memory backend and unit-testable offline.

Everything is scoped to one ``index_version`` (resolved from an alias by the
service), so a bulk reindex into a fresh version never disturbs live reads.

A tiny idempotent DDL bootstrap (:meth:`ensure_schema`) adds the generated
``search_vector`` column + the GIN/HNSW indexes when the table was created by
``Base.metadata.create_all`` (the test path) rather than by the Alembic
migration — so integration tests against a ``create_all`` schema still exercise
the real FTS path.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.search import SEARCH_VECTOR_SQL, SearchDocumentRow
from app.search.analyzer import Analyzer, default_analyzer
from app.search.documents import DocKind, SearchDocument
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
from app.search.query import FieldFilter, Occur, ParsedQuery, RangeFilter, RangeOp
from app.search.ranking import RankedList, reciprocal_rank_fusion

# The pool of candidates each arm fetches before fusion. A larger pool improves
# recall of items that rank well in one arm but poorly in the other.
_ARM_CANDIDATES = 200

#: Session-factory shape: an async context manager yielding an ``AsyncSession``.
SessionFactory = Any


class PostgresIndex:
    """Postgres FTS + pgvector hybrid index (satisfies ``SearchIndex``)."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        index_version: str = "v1",
        analyzer: Analyzer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._version = index_version
        self._analyzer = analyzer or default_analyzer()

    @property
    def index_version(self) -> str:
        """The index version this backend reads/writes."""
        return self._version

    def for_version(self, index_version: str) -> PostgresIndex:
        """Return a backend bound to a different index version (for reindex swap)."""
        return PostgresIndex(
            self._session_factory, index_version=index_version, analyzer=self._analyzer
        )

    # -- DDL bootstrap (idempotent) ----------------------------------------- #

    async def ensure_schema(self) -> None:
        """Add the generated tsvector column + GIN/HNSW indexes if missing.

        A no-op when the Alembic migration already created them. This keeps the
        backend usable against a ``create_all`` schema (the integration-test path,
        which doesn't run migrations) without ever duplicating an existing object.
        """
        async with self._session_factory() as session:
            has_col = await session.scalar(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'search_documents' AND column_name = 'search_vector'"
                )
            )
            if not has_col:
                await session.execute(
                    text(
                        "ALTER TABLE search_documents ADD COLUMN search_vector tsvector "
                        f"GENERATED ALWAYS AS ({SEARCH_VECTOR_SQL}) STORED"
                    )
                )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_documents_search_vector "
                    "ON search_documents USING gin (search_vector)"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_search_documents_embedding "
                    "ON search_documents USING hnsw (embedding vector_cosine_ops) "
                    "WITH (m = 16, ef_construction = 64)"
                )
            )

    # -- mutation ----------------------------------------------------------- #

    async def upsert(self, documents: Iterable[SearchDocument]) -> int:
        rows = [self._to_values(doc) for doc in documents]
        if not rows:
            return 0
        async with self._session_factory() as session:
            for batch in _chunks(rows, 200):
                stmt = pg_insert(SearchDocumentRow).values(batch)
                mutable = {
                    c: getattr(stmt.excluded, c)
                    for c in (
                        "kind",
                        "ref_id",
                        "book_id",
                        "title",
                        "body",
                        "keywords_text",
                        "facets",
                        "numbers",
                        "payload",
                        "embedding",
                        "boost",
                    )
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=["index_version", "doc_id"], set_=mutable
                )
                await session.execute(stmt)
        return len(rows)

    async def delete(self, doc_ids: Iterable[str]) -> int:
        ids = list(doc_ids)
        if not ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "DELETE FROM search_documents "
                    "WHERE index_version = :v AND doc_id = ANY(:ids)"
                ),
                {"v": self._version, "ids": ids},
            )
            return int(result.rowcount or 0)

    async def delete_by_book(self, book_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "DELETE FROM search_documents "
                    "WHERE index_version = :v AND book_id = :b"
                ),
                {"v": self._version, "b": book_id},
            )
            return int(result.rowcount or 0)

    async def clear(self) -> None:
        async with self._session_factory() as session:
            await session.execute(
                text("DELETE FROM search_documents WHERE index_version = :v"),
                {"v": self._version},
            )

    async def count(self, *, book_id: str | None = None) -> int:
        async with self._session_factory() as session:
            if book_id is None:
                n = await session.scalar(
                    text("SELECT count(*) FROM search_documents WHERE index_version = :v"),
                    {"v": self._version},
                )
            else:
                n = await session.scalar(
                    text(
                        "SELECT count(*) FROM search_documents "
                        "WHERE index_version = :v AND book_id = :b"
                    ),
                    {"v": self._version, "b": book_id},
                )
            return int(n or 0)

    # -- search ------------------------------------------------------------- #

    async def search(self, request: SearchRequest) -> SearchResponse:
        start = time.perf_counter()
        async with self._session_factory() as session:
            lexical = (
                await self._lexical_arm(session, request)
                if request.mode is not SearchMode.SEMANTIC
                else []
            )
            semantic = (
                await self._semantic_arm(session, request)
                if request.mode is not SearchMode.LEXICAL
                and request.query_embedding is not None
                else []
            )
            fused = self._fuse(request, lexical, semantic)
            total = len(fused)
            page_ids = [d for d, _ in fused[request.offset : request.offset + request.limit]]
            score_map = dict(fused)
            lex_pos = {d: i for i, d in enumerate(lexical)}
            sem_pos = {d: i for i, d in enumerate(semantic)}
            rows = await self._load_rows(session, page_ids)
            hits = [
                self._make_hit(
                    rows[d],
                    score=score_map.get(d, 0.0),
                    request=request,
                    lexical_rank=lex_pos.get(d),
                    semantic_rank=sem_pos.get(d),
                )
                for d in page_ids
                if d in rows
            ]
            facets = await self._facets(session, request, fused)
        took = (time.perf_counter() - start) * 1000.0
        return SearchResponse(
            hits=hits, total=total, facets=facets, took_ms=round(took, 3), mode=request.mode
        )

    async def _lexical_arm(self, session: AsyncSession, request: SearchRequest) -> list[str]:
        where, params = self._scope_sql(request)
        if request.query.has_text:
            tsquery_expr = self._tsquery_expr(request.query, params)
            if tsquery_expr is None:
                return []
            where.append(f"search_vector @@ ({tsquery_expr})")
            order = f"ts_rank_cd(search_vector, ({tsquery_expr})) DESC, boost DESC"
        else:
            # Filter/facet-only query: rank by boost so facets still return a page.
            order = "boost DESC, doc_id ASC"
        sql = (
            "SELECT doc_id FROM search_documents WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order} LIMIT :lim"
        )
        params["lim"] = _ARM_CANDIDATES
        result = await session.execute(text(sql), params)
        return [row[0] for row in result.all()]

    async def _semantic_arm(self, session: AsyncSession, request: SearchRequest) -> list[str]:
        where, params = self._scope_sql(request)
        where.append("embedding IS NOT NULL")
        vec = list(request.query_embedding or [])
        literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
        sql = (
            "SELECT doc_id FROM search_documents WHERE "
            + " AND ".join(where)
            + f" ORDER BY embedding <=> '{literal}'::vector ASC LIMIT :lim"
        )
        params["lim"] = _ARM_CANDIDATES
        result = await session.execute(text(sql), params)
        return [row[0] for row in result.all()]

    def _scope_sql(self, request: SearchRequest) -> tuple[list[str], dict[str, Any]]:
        where = ["index_version = :v"]
        params: dict[str, Any] = {"v": self._version}
        if request.book_id is not None:
            where.append("book_id = :book_id")
            params["book_id"] = request.book_id
        if request.kinds:
            where.append("kind = ANY(:kinds)")
            params["kinds"] = [k.value for k in request.kinds]
        self._filter_sql(request.query, where, params)
        self._range_sql(request.query, where, params)
        return where, params

    def _filter_sql(
        self, query: ParsedQuery, where: list[str], params: dict[str, Any]
    ) -> None:
        for i, f in enumerate(query.filters):
            self._one_filter_sql(f, i, where, params)

    def _one_filter_sql(
        self, f: FieldFilter, i: int, where: list[str], params: dict[str, Any]
    ) -> None:
        key = f"f{i}"
        if f.field == "kind":
            cond = f"lower(kind) = lower(:{key})"
        elif f.field == "book_id":
            cond = f"book_id = :{key}"
        else:
            field = _safe_ident(f.field)
            cond = f"lower(facets->>'{field}') = lower(:{key})"
        params[key] = f.value
        where.append(f"NOT ({cond})" if f.negate else cond)

    def _range_sql(
        self, query: ParsedQuery, where: list[str], params: dict[str, Any]
    ) -> None:
        for i, r in enumerate(query.ranges):
            self._one_range_sql(r, i, where, params)

    def _one_range_sql(
        self, r: RangeFilter, i: int, where: list[str], params: dict[str, Any]
    ) -> None:
        col = f"(numbers->>'{_safe_ident(r.field)}')::float8"
        if r.lo is not None:
            params[f"rlo{i}"] = r.lo
            where.append(f"{col} >= :rlo{i}")
        if r.hi is not None:
            params[f"rhi{i}"] = r.hi
            where.append(f"{col} <= :rhi{i}")
        if r.op is not None and r.value is not None:
            params[f"rv{i}"] = r.value
            op_sql = {
                RangeOp.GT: ">",
                RangeOp.GTE: ">=",
                RangeOp.LT: "<",
                RangeOp.LTE: "<=",
                RangeOp.EQ: "=",
            }[r.op]
            where.append(f"{col} {op_sql} :rv{i}")

    def _tsquery_expr(self, query: ParsedQuery, params: dict[str, Any]) -> str | None:
        """Build a parametrized ``tsquery`` SQL expression from the parsed clauses.

        Combines per-clause ``plainto_tsquery`` / ``phraseto_tsquery`` calls with
        the boolean tsquery operators (``||`` OR, ``&&`` AND, ``!!`` NOT) so the
        boolean *semantics* match the in-memory backend exactly:

            (should1 || should2 || …) && must1 && … && !!mustnot1 && …

        Postgres does the stemming (via ``plainto_tsquery('english', …)``), and
        every clause term is a bound parameter — there is no string interpolation
        of user input, only of the safe ``&&``/``||``/``!!`` operators.
        """
        should: list[str] = []
        must: list[str] = []
        must_not: list[str] = []

        def add(text_value: str, occur: Occur, func: str) -> None:
            value = text_value.replace('"', " ").strip()
            if not value:
                return
            key = f"ts{len(params)}"
            params[key] = value
            expr = f"{func}('english', :{key})"
            if occur is Occur.MUST:
                must.append(expr)
            elif occur is Occur.MUST_NOT:
                must_not.append(expr)
            else:
                should.append(expr)

        for t in query.terms:
            add(t.text, t.occur, "plainto_tsquery")
        for p in query.phrases:
            add(p.text, p.occur, "phraseto_tsquery")

        clauses: list[str] = []
        if should:
            clauses.append("(" + " || ".join(should) + ")")
        clauses.extend(must)
        if not clauses and not must_not:
            return None
        # A pure must-not query needs *something* positive to subtract from; an
        # empty positive side matches nothing, which is the correct semantics.
        positive = " && ".join(clauses) if clauses else None
        negatives = " && ".join(f"!!({m})" for m in must_not)
        if positive and negatives:
            return f"({positive}) && {negatives}"
        if positive:
            return positive
        return None

    def _fuse(
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

    async def _load_rows(
        self, session: AsyncSession, doc_ids: Sequence[str]
    ) -> dict[str, SearchDocumentRow]:
        if not doc_ids:
            return {}
        stmt = (
            text(
                "SELECT * FROM search_documents "
                "WHERE index_version = :v AND doc_id = ANY(:ids)"
            )
            .bindparams(bindparam("ids", expanding=False))
        )
        result = await session.execute(
            stmt, {"v": self._version, "ids": list(doc_ids)}
        )
        out: dict[str, SearchDocumentRow] = {}
        for row in result.mappings().all():
            out[row["doc_id"]] = SearchDocumentRow(**{k: row[k] for k in _ROW_FIELDS if k in row})
        return out

    async def _facets(
        self, session: AsyncSession, request: SearchRequest, fused: list[tuple[str, float]]
    ) -> list[Facet]:
        if not request.facet_fields or not fused:
            return []
        ids = [d for d, _ in fused]
        facets: list[Facet] = []
        for fname in request.facet_fields:
            if fname == "kind":
                expr = "kind"
            elif fname == "book_id":
                expr = "book_id"
            else:
                expr = f"facets->>'{_safe_ident(fname)}'"
            sql = (
                f"SELECT {expr} AS v, count(*) AS c FROM search_documents "
                "WHERE index_version = :v AND doc_id = ANY(:ids) "
                f"AND {expr} IS NOT NULL GROUP BY v ORDER BY c DESC, v ASC LIMIT 50"
            )
            result = await session.execute(
                text(sql), {"v": self._version, "ids": ids}
            )
            counts = [FacetCount(value=str(r[0]), count=int(r[1])) for r in result.all()]
            facets.append(Facet(field=fname, counts=counts))
        return facets

    def _make_hit(
        self,
        row: SearchDocumentRow,
        *,
        score: float,
        request: SearchRequest,
        lexical_rank: int | None,
        semantic_rank: int | None,
    ) -> SearchHit:
        highlights: list[FieldHighlight] = []
        if request.highlight and request.query.has_text:
            terms = set(self._analyzer.analyze(request.query.free_text))
            for fname, value in (("title", row.title), ("body", row.body)):
                if not value:
                    continue
                snippet = highlight(value, terms, analyzer=self._analyzer)
                if "<mark>" in snippet.text or fname == "title":
                    highlights.append(FieldHighlight(field=fname, snippet=snippet.text))
        return SearchHit(
            doc_id=row.doc_id,
            kind=DocKind(row.kind),
            ref_id=row.ref_id,
            book_id=row.book_id,
            score=round(score, 6),
            title=row.title,
            highlights=highlights,
            lexical_rank=lexical_rank,
            semantic_rank=semantic_rank,
            payload=dict(row.payload or {}),
        )

    async def suggest(self, prefix: str, *, limit: int = 8) -> list[str]:
        """Prefix autocomplete: tokens from the index sharing the analyzed prefix.

        Pulls candidate text from rows whose title/keywords/body contain the raw
        prefix (an indexed-friendly ``ILIKE``), then extracts and analyzes the
        matching tokens in Python — avoiding any SQL string interpolation of the
        prefix and degrading gracefully if the scan is large.
        """
        raw = prefix.lower().strip().split()[-1] if prefix.strip() else ""
        analyzed = self._analyzer.analyze(prefix)
        base = analyzed[0] if analyzed else raw
        if not base:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT title, keywords_text, body FROM search_documents "
                    "WHERE index_version = :v AND ("
                    "  title ILIKE :p OR keywords_text ILIKE :p OR body ILIKE :p) "
                    "LIMIT 500"
                ),
                {"v": self._version, "p": f"%{raw}%"},
            )
            seen: set[str] = set()
            scored: list[str] = []
            for title, kw, body in result.all():
                for chunk in (title, kw, body):
                    if not chunk:
                        continue
                    for term in self._analyzer.analyze(chunk):
                        if (term.startswith(base) or term.startswith(raw)) and term not in seen:
                            seen.add(term)
                            scored.append(term)
            scored.sort(key=lambda t: (len(t), t))
            return scored[:limit]

    def _to_values(self, doc: SearchDocument) -> dict[str, Any]:
        return {
            "index_version": self._version,
            "doc_id": doc.doc_id,
            "kind": doc.kind.value,
            "ref_id": doc.ref_id,
            "book_id": doc.book_id,
            "title": doc.title or "",
            "body": doc.body or "",
            "keywords_text": " ".join(doc.keywords),
            "facets": dict(doc.facets),
            "numbers": dict(doc.numbers),
            "payload": dict(doc.payload),
            "embedding": doc.embedding,
            "boost": float(doc.payload.get("boost", 1.0)),
        }


_ROW_FIELDS = (
    "index_version",
    "doc_id",
    "kind",
    "ref_id",
    "book_id",
    "title",
    "body",
    "keywords_text",
    "facets",
    "numbers",
    "payload",
    "embedding",
    "boost",
)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Validate a JSONB-key / column identifier before it is interpolated into SQL.

    Facet / numeric field names come from the parser's constrained allowlists, but
    this is the defence-in-depth gate: a name that isn't a plain identifier raises
    rather than reaching the query string (no injection surface).
    """
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe search field identifier: {name!r}")
    return name


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = ["PostgresIndex"]
