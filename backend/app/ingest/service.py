"""Phase A orchestration — ingest a book end-to-end, token-only (§9.1).

:func:`ingest_pdf` runs the whole cheap, global, zero-video-second import:

    importing → extract → analyse (+ canon build) → shot list + source-span index
    → identity lock → ready

Each milestone fires the async ``progress(stage, pct)`` callback (the API/SSE
layer forwards these to the shelf progress strip, §5.1). The pipeline is robust:
on any failure the book is moved to ``failed`` and the error logged; the heavy
steps each run in their own committed unit-of-work so a partial import is
resumable, and extraction is idempotent (already-extracted pages are not
re-inserted). No video is generated here — only text, image-gen keyframes, and
preset-voice assignment.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

import anyio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.adapter import Adapter
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import BookStatus
from app.db.models.ingest_checkpoint import IngestMilestone
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo, SourceSpanRepo
from app.db.session import get_session
from app.ingest.analyze import DEFAULT_CONCURRENCY, analyze_pages
from app.ingest.canon_build import (
    DEFAULT_ART_DIRECTION,
    DEFAULT_LENS,
    DEFAULT_PALETTE,
    build_canon,
)
from app.ingest.checkpoints import clear_checkpoints, record_milestone
from app.ingest.identity_lock import lock_identities
from app.ingest.ocr import OcrEngine, VlOcrEngine
from app.ingest.pdf_extract import DEFAULT_DPI, DEFAULT_OCR_WORD_FLOOR, extract_pdf
from app.ingest.shot_plan import plan_and_persist
from app.memory.canon_service import CanonService
from app.memory.interfaces import BlobStore
from app.providers import Providers
from app.storage.object_store import ObjectStore

logger = get_logger("app.ingest.service")

#: ``async progress(stage: str, pct: float)`` invoked at each milestone.
ProgressCallback = Callable[[str, float], Awaitable[None]]
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class IngestError(RuntimeError):
    """Raised when ingest cannot start (e.g. unknown book / missing PDF)."""


@dataclass(frozen=True, slots=True)
class IngestOptions:
    """Tuning knobs for one ingest run (all with production-sane defaults)."""

    dpi: int = DEFAULT_DPI
    pages_per_scene: int = 1
    analyze_concurrency: int = DEFAULT_CONCURRENCY
    analyze_max_tokens: int = 1500
    adapter_max_tokens: int | None = 1500
    poses: tuple[str, ...] = ("front",)
    #: A character is identity-locked when it appears in ≥ this many beats.
    min_beats: int = 2
    #: qwen-image-plus only accepts a fixed size set
    #: (1664*928, 1472*1104, 1328*1328, 1104*1472, 928*1664); 928*1664 is the
    #: 9:16 option matching the vertical FILM_SIZE (720x1280).
    keyframe_size: str = "928*1664"
    #: Re-run even if the book is already ``ready``.
    force: bool = False
    #: OCR fallback for scanned/image-only pages (defaults follow settings).
    ocr_enabled: bool | None = None
    ocr_word_floor: int = DEFAULT_OCR_WORD_FLOOR
    #: Record durable per-milestone checkpoints so a resumed ingest can report /
    #: skip what it already finished (defaults follow settings).
    checkpoints_enabled: bool | None = None


class IngestResult(BaseModel):
    """Compact summary of an ingest run (counts + the locked principals)."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    status: str
    num_pages: int = 0
    total_words: int = 0
    num_ocr_pages: int = 0
    num_entities: int = 0
    num_states: int = 0
    num_scenes: int = 0
    num_beats: int = 0
    num_shots: int = 0
    num_spans: int = 0
    principals: list[str] = Field(default_factory=list)


@dataclass
class _Context:
    """Shared dependencies threaded through the phases of one run."""

    book_id: str
    providers: Providers
    store: BlobStore
    settings: Settings
    session_factory: SessionFactory
    options: IngestOptions
    progress: ProgressCallback | None = None
    art_direction: str | None = None
    #: Optional publisher-supplied cover ``(bytes, content_type)`` (EPUB) used as
    #: page 1's image; ``None`` for PDFs (page 1 is the rendered first page).
    cover_image: tuple[bytes, str] | None = None
    _adapter: Adapter | None = field(default=None, repr=False)

    @property
    def adapter(self) -> Adapter:
        if self._adapter is None:
            self._adapter = Adapter(self.providers, settings=self.settings)
        return self._adapter

    @property
    def ocr_enabled(self) -> bool:
        """Whether OCR fallback is on for this run (option overrides setting)."""
        if self.options.ocr_enabled is not None:
            return self.options.ocr_enabled
        return self.settings.ingest_ocr_enabled

    @property
    def checkpoints_enabled(self) -> bool:
        """Whether durable milestone checkpoints are recorded (option > setting)."""
        if self.options.checkpoints_enabled is not None:
            return self.options.checkpoints_enabled
        return self.settings.ingest_checkpoints_enabled

    def ocr_engine(self) -> OcrEngine | None:
        """Build the VL-backed OCR engine when OCR is enabled, else ``None``."""
        if not self.ocr_enabled:
            return None
        return VlOcrEngine(self.providers.vl, max_tokens=self.settings.ingest_ocr_max_tokens)


async def _emit(ctx: _Context, stage: str, pct: float) -> None:
    """Fire the progress callback, swallowing callback errors (never fail ingest)."""
    if ctx.progress is None:
        return
    try:
        await ctx.progress(stage, pct)
    except Exception as exc:  # noqa: BLE001 - a flaky listener must not fail ingest
        logger.warning("ingest.progress.callback_failed", stage=stage, error=str(exc))


async def _checkpoint(
    ctx: _Context, milestone: IngestMilestone, **payload: object
) -> None:
    """Record a completed milestone (best-effort; never fails ingest)."""
    if not ctx.checkpoints_enabled:
        return
    await record_milestone(ctx.session_factory, ctx.book_id, milestone, payload=dict(payload))


async def ingest_pdf(
    book_id: str,
    pdf_bytes: bytes,
    *,
    providers: Providers,
    blob_store: BlobStore | None = None,
    settings: Settings | None = None,
    session_factory: SessionFactory = get_session,
    progress: ProgressCallback | None = None,
    options: IngestOptions | None = None,
    cover_image: tuple[bytes, str] | None = None,
) -> IngestResult:
    """Ingest ``pdf_bytes`` for ``book_id`` end-to-end (Phase A).

    Args:
        book_id: an existing book row (created at upload time).
        pdf_bytes: the raw PDF — for an EPUB upload, the PyMuPDF EPUB→PDF
            normalisation (:mod:`app.ingest.epub_extract`), so PDF and EPUB share
            this one pipeline.
        providers: live provider bundle (VL, image, embeddings).
        blob_store: object store; defaults to :meth:`ObjectStore.from_settings`.
        settings: app settings (defaults to the process settings).
        session_factory: unit-of-work factory (overridden in tests).
        progress: optional async ``(stage, pct)`` milestone callback.
        options: tuning knobs.
        cover_image: optional ``(bytes, content_type)`` for a publisher-supplied
            cover (an EPUB's declared cover image), used as page 1's image.

    Returns:
        A compact :class:`IngestResult`.

    Raises:
        IngestError: if the book row does not exist.
    """
    settings = settings or get_settings()
    options = options or IngestOptions()
    store: BlobStore = blob_store or ObjectStore.from_settings(settings)
    ctx = _Context(
        book_id=book_id,
        providers=providers,
        store=store,
        settings=settings,
        session_factory=session_factory,
        options=options,
        progress=progress,
        cover_image=cover_image,
    )

    await _emit(ctx, "importing", 0.0)
    async with session_factory() as session:
        book = await BookRepo(session).get(book_id)
        if book is None:
            raise IngestError(f"unknown book: {book_id}")
        if book.status == BookStatus.READY and not options.force:
            logger.info("ingest.skip.already_ready", book_id=book_id)
            return IngestResult(
                book_id=book_id, status=BookStatus.READY.value, num_pages=book.num_pages or 0
            )
        ctx.art_direction = book.art_direction
        await BookRepo(session).set_status(book_id, BookStatus.IMPORTING)

    # A forced re-ingest re-runs every stage, so any stale milestone ledger from
    # a prior (possibly different-content) run must be cleared first.
    if options.force and ctx.checkpoints_enabled:
        await clear_checkpoints(session_factory, book_id)

    try:
        result = await _run_pipeline(ctx, pdf_bytes)
    except Exception:
        logger.exception("ingest.failed", book_id=book_id)
        async with session_factory() as session:
            await BookRepo(session).set_status(book_id, BookStatus.FAILED)
        await _emit(ctx, "failed", 1.0)
        raise

    await _emit(ctx, "ready", 1.0)
    return result


async def ingest_book(
    book_id: str,
    *,
    providers: Providers,
    blob_store: BlobStore | None = None,
    settings: Settings | None = None,
    session_factory: SessionFactory = get_session,
    progress: ProgressCallback | None = None,
    options: IngestOptions | None = None,
) -> IngestResult:
    """Load the book's stored PDF (``book.source_pdf_key``) and ingest it."""
    settings = settings or get_settings()
    store: BlobStore = blob_store or ObjectStore.from_settings(settings)
    async with session_factory() as session:
        book = await BookRepo(session).get(book_id)
        if book is None:
            raise IngestError(f"unknown book: {book_id}")
        pdf_key = book.source_pdf_key
    if not pdf_key:
        raise IngestError(f"book has no source_pdf_key: {book_id}")
    pdf_bytes = await anyio.to_thread.run_sync(store.get_bytes, pdf_key)
    return await ingest_pdf(
        book_id,
        pdf_bytes,
        providers=providers,
        blob_store=store,
        settings=settings,
        session_factory=session_factory,
        progress=progress,
        options=options,
    )


class ReingestPlan(BaseModel):
    """The outcome of diffing a changed source against the persisted pages (§9.1).

    A re-ingest can use this to decide whether to skip (identical), do a full
    re-ingest (heavily changed), or — when an incremental path is wired — touch
    only ``pages_to_reanalyze``.
    """

    model_config = ConfigDict(extra="forbid")

    book_id: str
    identical: bool
    full_reingest: bool
    num_unchanged: int = 0
    num_changed: int = 0
    num_added: int = 0
    num_removed: int = 0
    pages_to_reanalyze: list[int] = Field(default_factory=list)


def _new_pdf_page_texts(pdf_bytes: bytes) -> dict[int, str]:
    """Cheap text-only pass over a new PDF (no rendering) for the re-ingest diff."""
    import fitz  # PyMuPDF — local import keeps the module import light.

    out: dict[int, str] = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index in range(doc.page_count):
            out[index + 1] = doc.load_page(index).get_text("text") or ""
    return out


async def plan_reingest(
    book_id: str,
    new_pdf_bytes: bytes,
    *,
    session_factory: SessionFactory = get_session,
    changed_fraction_threshold: float = 0.5,
) -> ReingestPlan:
    """Diff a changed source against the persisted pages and recommend an action.

    Loads the book's persisted page texts, extracts the new PDF's per-page text
    (cheap — no rasterisation), and computes a :class:`app.ingest.diff.IngestDiff`.
    Pure of any mutation: it only *reads* and recommends, so a caller can decide
    to skip, re-ingest fully, or (future) re-analyse just the changed slice.
    """
    from app.ingest.diff import diff_pages, should_full_reingest

    async with session_factory() as session:
        rows = await PageRepo(session).list_for_book(book_id)
    old_texts: dict[int, str | None] = {r.page_number: r.text for r in rows}
    new_texts: dict[int, str | None] = dict(_new_pdf_page_texts(new_pdf_bytes))

    diff = diff_pages(old_texts, new_texts)
    return ReingestPlan(
        book_id=book_id,
        identical=diff.is_identical,
        full_reingest=should_full_reingest(
            diff, changed_fraction_threshold=changed_fraction_threshold
        ),
        num_unchanged=len(diff.unchanged),
        num_changed=len(diff.changed),
        num_added=len(diff.added),
        num_removed=len(diff.removed),
        pages_to_reanalyze=diff.to_reanalyze,
    )


async def _run_pipeline(ctx: _Context, pdf_bytes: bytes) -> IngestResult:
    """Run extract → analyse → canon → shot-plan → identity-lock and mark ready."""
    opts = ctx.options

    # 1) Extract (own UoW; idempotent + streaming/bounded-memory). A page-level
    #    progress callback maps page N/total into the [0.0, 0.2) extract band so
    #    the shelf strip advances smoothly across a very large book.
    async def _page_progress(current: int, total: int) -> None:
        frac = (current / total) if total else 1.0
        await _emit(ctx, "extract", round(min(0.2, 0.2 * frac), 4))

    async with ctx.session_factory() as session:
        extract = await extract_pdf(
            PageRepo(session),
            book_id=ctx.book_id,
            pdf_bytes=pdf_bytes,
            blob_store=ctx.store,
            dpi=opts.dpi,
            cover_image=ctx.cover_image,
            ocr_engine=ctx.ocr_engine(),
            ocr_word_floor=opts.ocr_word_floor,
            page_progress=_page_progress,
        )
        await BookRepo(session).set_num_pages(ctx.book_id, extract.num_pages)
    await _emit(ctx, "extract", 0.2)
    await _checkpoint(
        ctx,
        IngestMilestone.EXTRACT,
        num_pages=extract.num_pages,
        total_words=extract.total_words,
        num_ocr_pages=extract.num_ocr_pages,
    )

    # 2) Analyse pages (VL, bounded concurrency; no DB writes).
    analyses = await analyze_pages(
        extract.pages,
        providers=ctx.providers,
        blob_store=ctx.store,
        concurrency=opts.analyze_concurrency,
        max_tokens=opts.analyze_max_tokens,
        rate_per_s=ctx.settings.ingest_analyze_rate_per_s,
        rate_burst=ctx.settings.ingest_analyze_rate_burst,
        max_attempts=ctx.settings.ingest_analyze_max_attempts,
        backoff_base_s=ctx.settings.ingest_analyze_backoff_base_s,
    )
    await _emit(ctx, "analyze", 0.45)
    await _checkpoint(ctx, IngestMilestone.ANALYZE, num_pages=len(analyses))

    # 3) Build the canon (entities + Style node + initial states).
    async with ctx.session_factory() as session:
        canon = await build_canon(
            CanonService(session, embedder=ctx.providers.embeddings, blob_store=ctx.store),
            book_id=ctx.book_id,
            analyses=analyses,
            art_direction=ctx.art_direction,
        )
    await _emit(ctx, "canon", 0.6)
    await _checkpoint(
        ctx, IngestMilestone.CANON, num_entities=len(canon.entities), num_states=canon.num_states
    )

    # 4) Shot list + source-span index (the §4.2 backbone).
    async with ctx.session_factory() as session:
        plan = await plan_and_persist(
            book_id=ctx.book_id,
            extract=extract,
            canon=canon,
            adapter=ctx.adapter,
            scenes=SceneRepo(session),
            beats=BeatRepo(session),
            shots=ShotRepo(session),
            spans=SourceSpanRepo(session),
            pages_per_scene=opts.pages_per_scene,
            max_tokens=opts.adapter_max_tokens,
            max_attempts=ctx.settings.ingest_shotplan_max_attempts,
            backoff_base_s=ctx.settings.ingest_shotplan_backoff_base_s,
        )
    await _emit(ctx, "shot_plan", 0.8)
    await _checkpoint(
        ctx, IngestMilestone.SHOT_PLAN, num_shots=plan.num_shots, num_spans=plan.num_spans
    )

    # 5) Identity lock (keyframes + preset voices for principals).
    style_tokens = {
        "art_direction": ctx.art_direction or DEFAULT_ART_DIRECTION,
        "palette": DEFAULT_PALETTE,
        "lens": DEFAULT_LENS,
    }
    async with ctx.session_factory() as session:
        identity = await lock_identities(
            book_id=ctx.book_id,
            canon=CanonService(
                session, embedder=ctx.providers.embeddings, blob_store=ctx.store
            ),
            characters=canon.characters(),
            providers=ctx.providers,
            blob_store=ctx.store,
            style_tokens=style_tokens,
            poses=opts.poses,
            min_beats=opts.min_beats,
            keyframe_size=opts.keyframe_size,
            rate_per_s=ctx.settings.ingest_identity_rate_per_s,
            rate_burst=ctx.settings.ingest_identity_rate_burst,
            max_attempts=ctx.settings.ingest_identity_max_attempts,
            backoff_base_s=ctx.settings.ingest_identity_backoff_base_s,
        )
    await _emit(ctx, "identity_lock", 0.95)
    await _checkpoint(
        ctx, IngestMilestone.IDENTITY_LOCK, num_principals=len(identity.principals)
    )

    # 6) Ready.
    async with ctx.session_factory() as session:
        await BookRepo(session).set_status(ctx.book_id, BookStatus.READY)

    return IngestResult(
        book_id=ctx.book_id,
        status=BookStatus.READY.value,
        num_pages=extract.num_pages,
        total_words=extract.total_words,
        num_ocr_pages=extract.num_ocr_pages,
        num_entities=len(canon.entities),
        num_states=canon.num_states,
        num_scenes=len(plan.scene_ids),
        num_beats=plan.num_beats,
        num_shots=plan.num_shots,
        num_spans=plan.num_spans,
        principals=identity.principals,
    )


__all__ = [
    "IngestError",
    "IngestOptions",
    "IngestResult",
    "ProgressCallback",
    "ReingestPlan",
    "ingest_book",
    "ingest_pdf",
    "plan_reingest",
]
