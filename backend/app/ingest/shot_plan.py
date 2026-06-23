"""Shot list + source-span index — the §4.2 bridge from words to shots (§9.1 step 4).

This runs the real Adapter (``analyze_page`` → beats, ``plan_shots`` → shots),
groups beats into scenes, and **reconciles** every beat/shot to a REAL
book-global word range using the actual PyMuPDF page words — then persists
scenes, beats, shots and bulk-inserts the ``source_span_index`` rows.

Why reconciliation matters (the most important part of this phase):

The Adapter's ``source_span.word_range`` is best-effort — the model is told not
to laboriously count words (§10) — so it cannot be trusted as the index key.
The authoritative indices come from extraction. For every page we:

1. take the page's contiguous slice of the book-global word index (from
   :mod:`app.ingest.pdf_extract`);
2. give each beat on the page a **contiguous, ordered, non-overlapping** sub-range
   of that slice. The split is proportional, but *refined* by locating each
   beat's distinctive key phrases among the page's real words (so a beat lands on
   the words it actually depicts), with a clean proportional fallback when no
   phrase matches;
3. set each beat's ``source_span`` to its reconciled global range and let the
   Adapter's deterministic ``plan_shots`` split that real range into ~5s shots —
   which therefore inherit correct global sub-ranges;
4. emit one ``source_span_index`` row per shot. Because pages are globally
   contiguous, beats partition each page, and shots partition each beat, the
   whole book is covered with no gaps — so ``resolve_word_to_shot`` (greatest
   ``word_index_start ≤ w``) returns the right shot for any focus word.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.agents.adapter import Adapter
from app.agents.contracts import Beat, SourceSpan
from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.db.repositories.beat import BeatRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from app.ingest.canon_build import CanonBuildResult, normalize_name
from app.ingest.pdf_extract import PdfExtractResult

logger = get_logger("app.ingest.shot_plan")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 3
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "with", "for", "from", "as", "is", "was", "were", "be", "been", "her", "his",
    "its", "she", "he", "they", "them", "that", "this", "into", "up", "out",
    "down", "over", "under", "their", "our", "you", "your", "it", "him", "had",
    "has", "have", "did", "do", "does", "then", "than", "when", "where", "who",
    "which", "what", "are", "not", "no", "so", "if", "off", "about", "after",
    "before", "again", "very",
})


# --------------------------------------------------------------------------- #
# Scene grouping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SceneGroup:
    """A contiguous run of pages forming one scene (the stitch boundary, §4.2)."""

    scene_index: int
    scene_id: str
    page_start: int
    page_end: int
    pages: tuple[int, ...]


def group_scenes(page_numbers: Sequence[int], *, pages_per_scene: int = 1) -> list[SceneGroup]:
    """Group ordered page numbers into scenes of up to ``pages_per_scene`` pages.

    ``scene_id`` here is the *local* (per-book) label (``scene_001``…). Persistence
    book-scopes it via :func:`scope_id` so the global ``scenes`` PK never collides
    across two books (every book's plan reuses the same local labels).
    """
    ordered = sorted(set(page_numbers))
    groups: list[SceneGroup] = []
    step = max(1, pages_per_scene)
    for index, start in enumerate(range(0, len(ordered), step)):
        chunk = tuple(ordered[start : start + step])
        groups.append(
            SceneGroup(
                scene_index=index + 1,
                scene_id=f"scene_{index + 1:03d}",
                page_start=chunk[0],
                page_end=chunk[-1],
                pages=chunk,
            )
        )
    return groups


def scope_id(book_id: str, local_id: str) -> str:
    """Book-scope a local plan id so it is unique across books (§4.2 backbone).

    Scene/beat/shot ids are sequence-derived local labels (``scene_001``,
    ``beat_0000``, ``beat_0000_shot_00``) reused identically by *every* book, while
    their table PKs (``pk_scenes``/``pk_beats``/``pk_shots``) are on ``id`` alone —
    so two books collide on the first scene. Prefixing the book id (pure-hex, so
    the ``_`` delimiter is unambiguous) makes the stored id per-book unique without
    changing how any consumer treats it (the id is an opaque string everywhere; no
    code parses its shape). Idempotent: an already-scoped id is returned unchanged,
    so re-running the plan for the same book reproduces identical ids.
    """
    prefix = f"{book_id}_"
    return local_id if local_id.startswith(prefix) else f"{prefix}{local_id}"


# --------------------------------------------------------------------------- #
# Source-span reconciliation (pure, unit-testable)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageWords:
    """A page's words in reading order, anchored at its book-global start index."""

    start: int
    texts: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.texts)


@dataclass(frozen=True)
class BeatSpanInput:
    """The minimal beat shape the reconciler needs: id, page, and matchable text."""

    beat_id: str
    page: int
    text: str


def _content_tokens(text: str) -> list[str]:
    """Distinctive lower-cased content tokens (drops stopwords/short tokens)."""
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS
    ]


def _anchor_positions(beats: Sequence[BeatSpanInput], page_tokens: list[str]) -> list[int | None]:
    """Earliest forward page position where each beat's key phrases first match."""
    anchors: list[int | None] = []
    cursor = 0
    n = len(page_tokens)
    for beat in beats:
        keys = set(_content_tokens(beat.text))
        anchor: int | None = None
        if keys:
            for pos in range(cursor, n):
                if page_tokens[pos] in keys:
                    anchor = pos
                    break
        anchors.append(anchor)
        if anchor is not None:
            cursor = anchor + 1
    return anchors


def _position_estimates(anchors: Sequence[int | None], n: int) -> list[float]:
    """Fill missing anchors by interpolation; pure proportional when none matched."""
    count = len(anchors)
    if count == 0:
        return []
    known = [(i, float(a)) for i, a in enumerate(anchors) if a is not None]
    if not known:
        if count == 1:
            return [0.0]
        return [i * (n - 1) / (count - 1) for i in range(count)]

    pos: list[float] = [0.0] * count
    for i, anchor in enumerate(anchors):
        if anchor is not None:
            pos[i] = float(anchor)
            continue
        prev = next(((pi, pa) for pi, pa in reversed(known) if pi < i), None)
        nxt = next(((ni, na) for ni, na in known if ni > i), None)
        if prev is not None and nxt is not None:
            (pi, pa), (ni, na) = prev, nxt
            pos[i] = pa + (na - pa) * (i - pi) / (ni - pi)
        elif prev is not None:
            pi, pa = prev
            pos[i] = pa + (i - pi)
        elif nxt is not None:
            ni, na = nxt
            pos[i] = na - (ni - i)
    # Clamp into range and enforce non-decreasing order.
    bound = max(n - 1, 0)
    running = 0.0
    for i in range(count):
        pos[i] = max(running, min(pos[i], float(bound)))
        running = pos[i]
    return pos


def _ranges_from_positions(pos: Sequence[float], n: int) -> list[tuple[int, int]]:
    """Turn monotonic position estimates into contiguous half-open ``[lo, hi)`` ranges."""
    count = len(pos)
    if count == 0:
        return []
    if n <= 0:
        return [(0, 0)] * count
    if n <= count:
        # Fewer words than beats: give the first n beats one word each, rest empty.
        return [(min(i, n), min(i + 1, n)) for i in range(count)]

    bounds = [0]
    for i in range(count - 1):
        mid = round((pos[i] + pos[i + 1]) / 2)
        lo_allowed = bounds[-1] + 1
        hi_allowed = n - (count - 1 - i)
        bounds.append(max(lo_allowed, min(mid, hi_allowed)))
    bounds.append(n)
    return [(bounds[i], bounds[i + 1]) for i in range(count)]


def reconcile_beat_word_ranges(
    beats: Sequence[BeatSpanInput],
    pages: Mapping[int, PageWords],
) -> dict[str, tuple[int, int]]:
    """Reconcile beats to REAL book-global word ranges (the §4.2 backbone).

    Returns ``{beat_id: (global_start, global_end_exclusive)}``. Per page the
    beats partition the page's word slice contiguously (anchored by phrase match,
    proportional otherwise), so the union of all ranges covers every extracted
    word exactly once.
    """
    by_page: dict[int, list[BeatSpanInput]] = defaultdict(list)
    for beat in beats:
        by_page[beat.page].append(beat)

    out: dict[str, tuple[int, int]] = {}
    for page_number, page_beats in by_page.items():
        page = pages.get(page_number)
        if page is None or page.count == 0:
            for beat in page_beats:
                out[beat.beat_id] = (page.start if page else 0, page.start if page else 0)
            continue
        tokens = [t.lower() for t in page.texts]
        anchors = _anchor_positions(page_beats, tokens)
        positions = _position_estimates(anchors, page.count)
        ranges = _ranges_from_positions(positions, page.count)
        for beat, (lo, hi) in zip(page_beats, ranges, strict=True):
            out[beat.beat_id] = (page.start + lo, page.start + hi)
    return out


# --------------------------------------------------------------------------- #
# Entity resolution (beat names → canon entity_keys)
# --------------------------------------------------------------------------- #


def _name_in_text(normalized_name: str, normalized_text: str) -> bool:
    """Whether a (possibly multi-word) normalised name appears in normalised text."""
    if not normalized_name:
        return False
    return f" {normalized_name} " in f" {normalized_text} "


def resolve_beat_entities(beat: Beat, alias_index: Mapping[str, str]) -> list[str]:
    """Resolve a beat's named entities to canon ``entity_key`` s (§10 no-invent).

    Combines the names the Adapter kept with a scan of the beat's summary +
    described visuals for any canon name/alias, so ``canon.query`` later returns
    exactly the characters/locations present in the beat.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for name in beat.entities:
        key = alias_index.get(normalize_name(name))
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    haystack = normalize_name(f"{beat.summary} {beat.described_visuals or ''}")
    for norm, key in alias_index.items():
        if key in seen:
            continue
        if _name_in_text(norm, haystack):
            seen.add(key)
            keys.append(key)
    return keys


# --------------------------------------------------------------------------- #
# Persistence orchestration
# --------------------------------------------------------------------------- #


class ShotPlanResult(BaseModel):
    """Outcome of shot planning + source-span indexing (for verification/telemetry)."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    scene_ids: list[str] = Field(default_factory=list)
    num_beats: int = 0
    num_shots: int = 0
    num_spans: int = 0
    #: beat_id -> (global_start, global_end_exclusive)
    beat_ranges: dict[str, tuple[int, int]] = Field(default_factory=dict)
    #: shot_id -> (global_start, global_end_exclusive)
    shot_ranges: dict[str, tuple[int, int]] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Repos:
    scenes: SceneRepo
    beats: BeatRepo
    shots: ShotRepo
    spans: SourceSpanRepo


async def plan_and_persist(
    *,
    book_id: str,
    extract: PdfExtractResult,
    canon: CanonBuildResult,
    adapter: Adapter,
    scenes: SceneRepo,
    beats: BeatRepo,
    shots: ShotRepo,
    spans: SourceSpanRepo,
    pages_per_scene: int = 1,
    max_tokens: int | None = 1500,
) -> ShotPlanResult:
    """Plan + persist scenes/beats/shots and the reconciled source-span index.

    Re-runnable: any prior plan for ``book_id`` is cleared first (within this
    unit of work), so resuming an ingest that previously failed *after* this step
    — e.g. a book that 429'd during identity-lock — re-inserts cleanly instead of
    colliding on ``pk_scenes``/``pk_shots`` (§9.1: "a partial import is resumable").
    """
    repos = _Repos(scenes=scenes, beats=beats, shots=shots, spans=spans)

    # Clear-then-insert: drop shots (cascading source_span_index) and scenes
    # (cascading beats) for this book before re-planning. Idempotent + atomic.
    await repos.shots.delete_for_book(book_id)
    await repos.scenes.delete_for_book(book_id)

    page_numbers = [p.page_number for p in extract.pages if p.num_words > 0]
    groups = group_scenes(page_numbers, pages_per_scene=pages_per_scene)
    # Book-scope every plan id (§4.2): the stored scene/beat/shot ids carry the
    # book id so the global PKs never collide across books, and a re-ingest is
    # idempotent (the same book reproduces identical scoped ids).
    page_to_scene = {pn: scope_id(book_id, g.scene_id) for g in groups for pn in g.pages}

    for group in groups:
        await repos.scenes.create(
            book_id=book_id,
            scene_index=group.scene_index,
            page_start=group.page_start,
            page_end=group.page_end,
            style_entity_key=canon.style_key,
            scene_id=scope_id(book_id, group.scene_id),
        )

    # --- Adapter: page text -> beats (book-global beat_index across pages) ---- #
    # ``analyze_page`` mints local beat ids (``beat_0000``…); we book-scope each
    # beat's id + its scene_id immediately, so the downstream reconcile keys,
    # ``plan_shots`` (which derives shot_id from beat_id and copies scene_id), and
    # the persisted rows are all consistently scoped from one place.
    all_beats: list[Beat] = []
    beat_index = 0
    for page in extract.pages:
        if page.num_words == 0:
            continue
        scene_id = page_to_scene[page.page_number]
        page_beats = await adapter.analyze_page(
            page.text,
            page=page.page_number,
            scene_id=scene_id,
            beat_index_start=beat_index,
            max_tokens=max_tokens,
        )
        for beat in page_beats:
            all_beats.append(
                beat.model_copy(update={"beat_id": scope_id(book_id, beat.beat_id)})
            )
        beat_index += len(page_beats)

    # --- Reconcile beats to REAL global word ranges -------------------------- #
    pages_words = {
        p.page_number: PageWords(
            start=p.word_index_start, texts=tuple(w.text for w in p.word_boxes)
        )
        for p in extract.pages
    }
    span_inputs = [
        BeatSpanInput(
            beat_id=b.beat_id,
            page=b.source_span.page or 0,
            text=f"{b.summary} {b.described_visuals or ''}",
        )
        for b in all_beats
    ]
    beat_ranges = reconcile_beat_word_ranges(span_inputs, pages_words)

    reconciled: list[Beat] = []
    for beat in all_beats:
        start, end = beat_ranges.get(beat.beat_id, (0, 0))
        span = beat.source_span.model_copy(update={"word_range": (start, end)})
        reconciled.append(beat.model_copy(update={"source_span": span}))

    # --- Persist beats with resolved canon entity_keys ----------------------- #
    for beat in reconciled:
        await repos.beats.create(
            book_id=book_id,
            scene_id=beat.scene_id or page_to_scene[beat.source_span.page],
            beat_index=beat.beat_index,
            summary=beat.summary,
            entities=resolve_beat_entities(beat, canon.alias_index),
            described_visuals=beat.described_visuals,
            mood=beat.mood,
            source_span=_span_dict(beat.source_span),
            beat_id=beat.beat_id,
        )

    # --- Adapter: beats -> shots (real global sub-ranges) -------------------- #
    shot_items = adapter.plan_shots(reconciled)
    shot_ranges: dict[str, tuple[int, int]] = {}
    span_rows: list[dict[str, object]] = []
    for item in shot_items:
        lo, hi = item.source_span.word_range
        shot_ranges[item.shot_id] = (lo, hi)
        await repos.shots.create(
            id=item.shot_id,
            book_id=book_id,
            scene_id=item.scene_id,
            beat_id=item.beat_id,
            source_span=_span_dict(item.source_span),
            status=ShotStatus.PLANNED,
            duration_s=item.est_duration_s,
            cost={"video_seconds": item.est_cost.video_seconds, "tokens": item.est_cost.tokens},
        )
        if hi > lo:  # skip empty (no-word) shots — they have nothing to resolve to
            span_rows.append(
                {
                    "book_id": book_id,
                    "word_index_start": lo,
                    "word_index_end": hi - 1,
                    "shot_id": item.shot_id,
                    "scene_id": item.scene_id,
                    "beat_id": item.beat_id,
                }
            )

    num_spans = await repos.spans.bulk_insert(span_rows) if span_rows else 0

    result = ShotPlanResult(
        book_id=book_id,
        scene_ids=[scope_id(book_id, g.scene_id) for g in groups],
        num_beats=len(reconciled),
        num_shots=len(shot_items),
        num_spans=num_spans,
        beat_ranges=beat_ranges,
        shot_ranges=shot_ranges,
    )
    logger.info(
        "ingest.shot_plan.done",
        book_id=book_id,
        scenes=len(groups),
        beats=result.num_beats,
        shots=result.num_shots,
        spans=result.num_spans,
    )
    return result


def _span_dict(span: SourceSpan) -> dict[str, object]:
    """Serialise a :class:`SourceSpan` to the JSONB shape stored on beats/shots."""
    return {"page": span.page, "para": span.para, "word_range": list(span.word_range)}


__all__ = [
    "BeatSpanInput",
    "PageWords",
    "SceneGroup",
    "ShotPlanResult",
    "group_scenes",
    "plan_and_persist",
    "reconcile_beat_word_ranges",
    "resolve_beat_entities",
    "scope_id",
]
