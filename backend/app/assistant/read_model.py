"""The canon read model — project the book's stores into retrievable spans.

The assistant grounds its answers in three of the §8 stores plus the page text:

* **pages** (``pages.text``) — the primary ground, chunked into passage spans;
* **canon entities** (``entities``) — character / location / prop / style
  descriptions, resolved *as of* the reader's beat (§8.1);
* **accepted shots** (``shots``) — the episodic store's narration (§8.2);
* **beats** (``beats.summary``) — compact recap atoms (§4.2).

:class:`CanonReadModel` is the seam the retriever depends on; it is a small,
read-only protocol so the real DB-backed :class:`DbCanonReadModel` *and* a test
fake both satisfy it without inheritance. The real implementation maps a page /
word position to a beat ordinal (so the spoiler gate can stamp page spans), and
stamps every span with the beat ordinal it belongs to.

Chunking is deliberately simple and deterministic (paragraph-ish windows with a
word budget) so the read model stays pure-ish and testable; the embedding /
re-rank lives in the retriever.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.types import ReadingPosition, RetrievedSpan, SourceKind
from app.db.models.beat import Beat
from app.db.models.book import Page
from app.db.models.entity import Entity
from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot

#: Default words-per-passage when chunking page text into spans.
DEFAULT_PASSAGE_WORDS = 90
#: Hard cap on candidate spans returned from each source (coarse recall).
DEFAULT_CANDIDATE_CAP = 200


class CanonReadModel(Protocol):
    """Read-only candidate-source seam the retriever depends on.

    Implementations return *unscored* candidate spans, already spoiler-stamped
    (each span's ``ordinal`` is the beat it belongs to). The retriever scores and
    re-ranks; the spoiler gate filters on ``ordinal``. Resolving a position to a
    beat ceiling is the read model's job because only it can see the DB.
    """

    async def resolve_ceiling_beat(self, position: ReadingPosition) -> int:
        """Resolve a reading position to its inclusive beat-ordinal ceiling."""
        ...

    async def candidate_spans(
        self,
        book_id: str,
        *,
        kinds: Sequence[SourceKind] | None = None,
        limit: int = DEFAULT_CANDIDATE_CAP,
    ) -> list[RetrievedSpan]:
        """Return unscored candidate spans across the requested source kinds."""
        ...


def chunk_passages(
    text: str, *, words_per_chunk: int = DEFAULT_PASSAGE_WORDS
) -> list[tuple[int, str]]:
    """Split page text into ``(start_word_index, passage)`` chunks (deterministic).

    Splits on blank-line paragraph boundaries first, then packs paragraphs into
    word-budgeted windows so a passage is a coherent unit but never blows the
    context budget. The ``start_word_index`` is the running word offset *within
    the page*, used to build the span locator and to map back to the source span.
    """
    if not text or not text.strip():
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    chunks: list[tuple[int, str]] = []
    word_cursor = 0
    buf: list[str] = []
    buf_words = 0
    buf_start = 0
    for para in paragraphs:
        n = len(para.split())
        if buf and buf_words + n > words_per_chunk:
            chunks.append((buf_start, "\n".join(buf)))
            buf, buf_words, buf_start = [], 0, word_cursor
        if not buf:
            buf_start = word_cursor
        buf.append(para)
        buf_words += n
        word_cursor += n
    if buf:
        chunks.append((buf_start, "\n".join(buf)))
    return chunks


class DbCanonReadModel:
    """The real :class:`CanonReadModel` over the pages / entities / shots / beats.

    Bound to a single :class:`AsyncSession` for the lifetime of one request. All
    reads are book-scoped; the caller is responsible for having checked the
    requester owns the book (the route does that before constructing this).
    """

    def __init__(
        self, session: AsyncSession, *, words_per_chunk: int = DEFAULT_PASSAGE_WORDS
    ) -> None:
        self._session = session
        self._words_per_chunk = words_per_chunk

    async def resolve_ceiling_beat(self, position: ReadingPosition) -> int:
        """Map a position to its inclusive beat ceiling (§8.5).

        An explicit ``beat_index`` wins. Otherwise we find the latest beat whose
        source span starts at or before the reader's word/page position — the
        beat the reader is currently inside — so a page-only client still gets a
        correct spoiler horizon. With nothing resolvable, ceiling 0 (book start).
        """
        if position.allow_full_book:
            return 1 << 62
        if position.beat_index is not None:
            return position.beat_index
        beats = await self._load_beats(position.book_id)
        if not beats:
            return 0
        word = position.word_index
        page = position.page
        best = 0
        for beat in beats:
            span = beat.source_span or {}
            beat_page = span.get("page")
            word_range = span.get("word_range") or []
            beat_word = word_range[0] if word_range else None
            by_word = word is not None and beat_word is not None and beat_word <= word
            by_page = page is not None and beat_page is not None and beat_page <= page
            if by_word or by_page:
                best = max(best, beat.beat_index)
        return best

    async def candidate_spans(
        self,
        book_id: str,
        *,
        kinds: Sequence[SourceKind] | None = None,
        limit: int = DEFAULT_CANDIDATE_CAP,
    ) -> list[RetrievedSpan]:
        """Gather unscored, spoiler-stamped candidate spans across sources."""
        wanted = set(kinds) if kinds else set(SourceKind)
        spans: list[RetrievedSpan] = []
        if SourceKind.PAGE in wanted:
            spans.extend(await self._page_spans(book_id))
        if SourceKind.BEAT in wanted:
            spans.extend(await self._beat_spans(book_id))
        if SourceKind.CANON in wanted:
            spans.extend(await self._canon_spans(book_id))
        if SourceKind.SHOT in wanted:
            spans.extend(await self._shot_spans(book_id))
        return spans[: max(0, limit)] if limit else spans

    # -- per-source projections --------------------------------------------- #

    async def _load_beats(self, book_id: str) -> list[Beat]:
        stmt = select(Beat).where(Beat.book_id == book_id).order_by(Beat.beat_index)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _page_word_to_beat(self, book_id: str) -> list[tuple[int, int | None, int]]:
        """Build a sorted ``(page, word_start, beat_index)`` map for stamping pages."""
        beats = await self._load_beats(book_id)
        out: list[tuple[int, int | None, int]] = []
        for beat in beats:
            span = beat.source_span or {}
            page = span.get("page")
            word_range = span.get("word_range") or []
            word_start = word_range[0] if word_range else None
            if page is not None:
                out.append((int(page), word_start, beat.beat_index))
        out.sort(key=lambda t: (t[0], t[1] if t[1] is not None else -1))
        return out

    async def _page_spans(self, book_id: str) -> list[RetrievedSpan]:
        stmt = (
            select(Page)
            .where(Page.book_id == book_id, Page.text.is_not(None))
            .order_by(Page.page_number)
        )
        pages = list((await self._session.execute(stmt)).scalars().all())
        page_beat_map = await self._page_word_to_beat(book_id)
        spans: list[RetrievedSpan] = []
        for page in pages:
            ordinal = self._beat_for_page(page.page_number, page_beat_map)
            for chunk_i, (word_start, passage) in enumerate(
                chunk_passages(page.text or "", words_per_chunk=self._words_per_chunk)
            ):
                spans.append(
                    RetrievedSpan(
                        span_id=f"page:{page.page_number}:{chunk_i}",
                        kind=SourceKind.PAGE,
                        text=passage,
                        ordinal=ordinal,
                        locator=f"p.{page.page_number}",
                        meta={"page": page.page_number, "word_start": word_start},
                    )
                )
        return spans

    @staticmethod
    def _beat_for_page(
        page_number: int, page_beat_map: list[tuple[int, int | None, int]]
    ) -> int:
        """Best-effort beat ordinal for a page (latest beat starting on/before it)."""
        ordinal = 0
        for page, _word, beat_index in page_beat_map:
            if page <= page_number:
                ordinal = max(ordinal, beat_index)
            else:
                break
        return ordinal

    async def _beat_spans(self, book_id: str) -> list[RetrievedSpan]:
        beats = await self._load_beats(book_id)
        spans: list[RetrievedSpan] = []
        for beat in beats:
            text = beat.summary or ""
            if beat.described_visuals:
                text = f"{text}\n{beat.described_visuals}"
            if not text.strip():
                continue
            spans.append(
                RetrievedSpan(
                    span_id=f"beat:{beat.beat_index}",
                    kind=SourceKind.BEAT,
                    text=text,
                    ordinal=beat.beat_index,
                    locator=f"beat {beat.beat_index}",
                    meta={"beat_index": beat.beat_index, "scene_id": beat.scene_id},
                )
            )
        return spans

    async def _canon_spans(self, book_id: str) -> list[RetrievedSpan]:
        # Every entity *version*; the retriever stamps the version's first valid
        # beat as the ordinal, so a character introduced at beat 30 can't surface
        # for a reader at beat 10.
        stmt = (
            select(Entity)
            .where(Entity.book_id == book_id)
            .order_by(Entity.entity_key, Entity.version.desc())
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        # Keep the highest version per key valid at-or-before the reader; but the
        # spoiler gate handles validity by ordinal, so emit the latest version per
        # key and stamp ordinal = valid_from_beat.
        seen: set[str] = set()
        spans: list[RetrievedSpan] = []
        for ent in rows:
            if ent.entity_key in seen:
                continue
            seen.add(ent.entity_key)
            desc = ent.description or ""
            appearance = (ent.appearance or {}).get("description") if ent.appearance else None
            text = " ".join(p for p in (ent.name, desc, appearance) if p).strip()
            if not text:
                continue
            spans.append(
                RetrievedSpan(
                    span_id=f"canon:{ent.entity_key}",
                    kind=SourceKind.CANON,
                    text=text,
                    ordinal=ent.valid_from_beat,
                    locator=f"{ent.name} ({ent.type.value})",
                    vector=list(ent.embedding) if ent.embedding is not None else None,
                    meta={
                        "entity_key": ent.entity_key,
                        "entity_type": ent.type.value,
                        "name": ent.name,
                        "aliases": ent.aliases or [],
                    },
                )
            )
        return spans

    async def _shot_spans(self, book_id: str) -> list[RetrievedSpan]:
        stmt = (
            select(Shot)
            .where(Shot.book_id == book_id, Shot.status == ShotStatus.ACCEPTED)
            .order_by(Shot.created_at)
        )
        shots = list((await self._session.execute(stmt)).scalars().all())
        beats = {b.id: b.beat_index for b in await self._load_beats(book_id)}
        spans: list[RetrievedSpan] = []
        for shot in shots:
            narration = (shot.narration or {}).get("text") if shot.narration else None
            text = narration or shot.prompt or ""
            if not text.strip():
                continue
            ordinal = beats.get(shot.beat_id or "", 0)
            spans.append(
                RetrievedSpan(
                    span_id=f"shot:{shot.id}",
                    kind=SourceKind.SHOT,
                    text=text,
                    ordinal=ordinal,
                    locator=f"film @ {shot.beat_id or shot.id}",
                    vector=list(shot.embedding) if shot.embedding is not None else None,
                    meta={"shot_id": shot.id, "beat_id": shot.beat_id},
                )
            )
        return spans


__all__ = [
    "DEFAULT_CANDIDATE_CAP",
    "DEFAULT_PASSAGE_WORDS",
    "CanonReadModel",
    "DbCanonReadModel",
    "chunk_passages",
]
