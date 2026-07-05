"""Book routes — upload + ingest trigger, shelf, pages, canon, shots (§5.1/§9.1).

``POST /books`` accepts a **PDF or EPUB** (§5.1's "PDF / EPUB upload"): it
validates the upload (content-type + content magic, size cap, sanitized
filename), normalises it to PDF — a PDF is stored verbatim, an EPUB is converted
to PDF with PyMuPDF (:mod:`app.ingest.epub_extract`) so both formats share the
one §9.1 extraction/analysis/render pipeline downstream — stores it under
``pdfs/`` (the original EPUB is kept under ``epubs/`` for provenance, and its
declared cover, if any, under ``covers/``), creates the ``importing`` book row,
records ownership, and triggers **Phase A** ingest out-of-band as a tracked
background task whose progress callback publishes events to the book's Redis
channel (the shelf progress strip). The read routes project the imported
artifacts: the shelf, a page (presigned image + text + word boxes for karaoke),
the human-inspectable canon vault, and the shot timeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy import select

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.schemas import (
    BookResponse,
    CanonAppearance,
    CanonEntityResponse,
    CanonReferenceImage,
    CanonResponse,
    CanonStateResponse,
    PageResponse,
    ShotResponse,
)
from app.composition import Container
from app.core.logging import get_logger
from app.db.base import new_id
from app.db.models.book import Book
from app.db.models.continuity import ContinuityState
from app.db.models.entity import Entity
from app.db.models.enums import BookStatus
from app.db.models.shot import Shot
from app.db.models.user import User
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.entity import EntityRepo
from app.ingest.epub_extract import (
    EPUB_CONTENT_TYPE,
    epub_page_count,
    epub_to_pdf_bytes,
    extract_epub_cover,
    extract_epub_metadata,
    looks_like_epub,
    sniff_image_content_type,
)
from app.memory.canon_vault import CanonVault
from app.queue.redis_queue import book_progress_key
from app.storage.object_store import keys

logger = get_logger("app.api.books")

router = APIRouter(prefix="/books", tags=["books"])

#: Hard upload size cap — bumped to 1GB to accommodate large illustrated PDFs
#: and full-novel EPUBs. The stream-and-abort read path (``_read_capped``) means
#: an oversized body still never fully buffers, but operators should monitor
#: worker RAM since a 1GB PDF parsed by PyMuPDF can spike memory significantly.
MAX_PDF_BYTES = 1024 * 1024 * 1024
#: Per-book page cap — bumped from 300 to 2000 so long novels and reference
#: works fit. This raises the worst-case Phase-A DashScope spend per book ~6.7×
#: (kinora.md §11.1); operators should pair this with per-tenant rate limits if
#: cost control is critical. Enforced after the extracted page count is known,
#: before any token-spending ingest is triggered.
MAX_INGEST_PAGES = 2000
#: Max books a single user may own — a coarse per-tenant ingest quota.
MAX_BOOKS_PER_USER = 50
#: Streaming read chunk; the upload body is read in chunks and aborted the moment
#: it crosses ``MAX_PDF_BYTES`` so an oversized body is never fully buffered.
_READ_CHUNK = 1 << 20  # 1 MiB
_PDF_MAGIC = b"%PDF-"
#: Accepted upload content-types: PDF, EPUB, and the generic octet-stream the apps
#: may send (the authoritative check is the content magic, not this header).
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        EPUB_CONTENT_TYPE,
        "application/epub",
        "application/octet-stream",
        "binary/octet-stream",
        "",
    }
)
#: Resolve canon "as of the latest version" — a beat beyond any real one, so the
#: still-open (current) version of every entity is returned.
_LATEST_BEAT = 2**31 - 1


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Read an upload in chunks, aborting once it exceeds ``cap`` bytes.

    Never buffers more than ``cap`` + one chunk: an oversized body is rejected
    mid-stream rather than read into memory in full (the upload-DoS fix).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise APIError(
                "file_too_large",
                "upload exceeds the size limit",
                status=413,
                detail={"max_bytes": cap},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _pdf_page_count(data: bytes) -> int:
    """Cheap page count via PyMuPDF (no rasterization) — the extraction count."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def _sanitize_filename(name: str | None) -> str:
    """Strip any path and unsafe characters from an uploaded filename."""
    base = os.path.basename(name or "").strip()
    safe = "".join(c for c in base if c.isalnum() or c in (" ", ".", "_", "-")).strip()
    return safe or "untitled"


def _title_from(filename: str, given: str | None) -> str:
    if given and given.strip():
        return given.strip()[:512]
    stem = os.path.splitext(_sanitize_filename(filename))[0]
    return (stem or "Untitled").replace("_", " ")[:512]


@dataclass(frozen=True, slots=True)
class _Normalized:
    """A validated, format-normalised upload ready to store + ingest.

    ``pdf_bytes`` is what feeds both ``source_pdf_key`` and the §9.1 extract step
    (a PDF verbatim, or an EPUB converted to PDF). ``epub_bytes`` is the original
    EPUB (kept for provenance) or ``None`` for a PDF upload. ``cover`` is the
    EPUB's declared cover ``(bytes, content_type)`` or ``None``. ``title`` /
    ``author`` are best-effort document metadata used only when the caller did
    not supply them.
    """

    pdf_bytes: bytes
    page_count: int
    epub_bytes: bytes | None
    cover: tuple[bytes, str] | None
    title: str | None
    author: str | None


def _normalize_pdf(data: bytes) -> _Normalized:
    """Validate a PDF upload (magic + page cap) — the bytes are used verbatim."""
    if not data[:1024].lstrip().startswith(_PDF_MAGIC):
        raise APIError("invalid_pdf", "file is not a valid PDF (missing %PDF header)", status=400)
    try:
        page_count = _pdf_page_count(data)
    except Exception as exc:  # noqa: BLE001 - any parse failure => not a usable PDF
        raise APIError("invalid_pdf", "file is not a readable PDF", status=400) from exc
    return _Normalized(
        pdf_bytes=data, page_count=page_count, epub_bytes=None, cover=None,
        title=None, author=None,
    )


def _normalize_epub(data: bytes) -> _Normalized:
    """Validate + normalise an EPUB upload to PDF (the shared §9.1 input).

    The EPUB is converted to PDF with PyMuPDF so it joins the PDF extraction
    path; its declared cover image and ``dc:title`` / ``dc:creator`` are pulled
    (best-effort) to seed the book cover and metadata. A conversion failure means
    the EPUB is not usable and is rejected like an unreadable PDF.
    """
    try:
        pdf_bytes = epub_to_pdf_bytes(data)
        page_count = epub_page_count(data)
    except Exception as exc:  # noqa: BLE001 - any parse failure => not a usable EPUB
        raise APIError("invalid_epub", "file is not a readable EPUB", status=400) from exc
    cover_raw = extract_epub_cover(data)
    cover = (
        (cover_raw[0], sniff_image_content_type(cover_raw[0], cover_raw[1]))
        if cover_raw is not None
        else None
    )
    meta_title, meta_author = extract_epub_metadata(data)
    return _Normalized(
        pdf_bytes=pdf_bytes, page_count=page_count, epub_bytes=data, cover=cover,
        title=meta_title, author=meta_author,
    )


def _normalize_upload(data: bytes) -> _Normalized:
    """Detect the upload format by content magic and normalise it to PDF bytes.

    Detection is content-based (not the spoofable content-type): ``%PDF-`` ⇒ PDF,
    the ``application/epub+zip`` ZIP signature ⇒ EPUB. Anything else is rejected.
    """
    if data[:1024].lstrip().startswith(_PDF_MAGIC):
        return _normalize_pdf(data)
    if looks_like_epub(data):
        return _normalize_epub(data)
    raise APIError(
        "unsupported_file",
        "file is neither a valid PDF nor a valid EPUB",
        status=400,
    )


async def _assert_owner(container: Container, user: User, book_id: str) -> Book:
    """Load a book the user owns, or 404.

    Ownership is the durable ``books.user_id`` column (the source of truth); a row
    with a NULL owner is accessible to nobody (fail-closed).
    """
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)
    return book


async def _book_response(container: Container, book: Book) -> BookResponse:
    progress = await container.redis.get_json(book_progress_key(book.id))
    pct: float | None = None
    stage: str | None = None
    if isinstance(progress, dict):
        pct = progress.get("pct")
        stage = progress.get("stage")
    elif book.status is BookStatus.READY:
        pct = 1.0
    # Project the durable cover_key as an ephemeral presigned URL (Agent 05).
    cover_url = (
        container.object_store.presigned_get_url(book.cover_key) if book.cover_key else None
    )
    return BookResponse(
        id=book.id,
        title=book.title,
        author=book.author,
        status=book.status.value,
        num_pages=book.num_pages,
        art_direction=book.art_direction,
        created_at=book.created_at.isoformat() if book.created_at else None,
        progress=pct,
        stage=stage,
        cover_url=cover_url,
    )


@router.post("", response_model=BookResponse, status_code=status.HTTP_201_CREATED)
async def upload_book(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="The source PDF or EPUB")],
    title: Annotated[str | None, Form()] = None,
    author: Annotated[str | None, Form()] = None,
    art_direction: Annotated[str | None, Form()] = None,
) -> BookResponse:
    """Validate + store a PDF/EPUB, create the book, and trigger Phase A ingest.

    Returns the freshly-created book (status ``importing``) directly — the shelf
    prepends it and polls until it is ``ready``.

    Defends the ingest pipeline: the content-type is checked, the per-user book
    quota is enforced, the body is read **streaming** (aborting the moment it
    crosses ``MAX_PDF_BYTES`` rather than buffering an oversized payload), the
    format is detected by content magic, an EPUB is normalised to PDF, and the
    page count is capped (``MAX_INGEST_PAGES``) before any token-spending ingest
    is triggered."""
    if file.content_type is not None and file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise APIError(
            "unsupported_media_type", "expected a PDF or EPUB upload", status=415,
            detail={"content_type": file.content_type},
        )

    # Per-user ingest quota (durable books.user_id), checked before reading the body.
    async with container.session_factory() as session:
        owned = await BookRepo(session).count_for_user(user.id)
    if owned >= MAX_BOOKS_PER_USER:
        raise APIError(
            "book_quota_exceeded",
            "per-user book limit reached",
            status=429,
            detail={"max_books": MAX_BOOKS_PER_USER},
        )

    # Stream the read, aborting once it exceeds the cap (no unbounded buffering).
    data = await _read_capped(file, MAX_PDF_BYTES)

    # Detect the format (PDF or EPUB) by content magic and normalise to PDF bytes
    # off the event loop (EPUB→PDF conversion is CPU-bound). The page cap is then
    # enforced on the extraction page count, before any ingest runs (so a
    # pathologically long upload can never drive unbounded DashScope spend, §11.1).
    norm = await anyio.to_thread.run_sync(_normalize_upload, data)
    if norm.page_count > MAX_INGEST_PAGES:
        raise APIError(
            "too_many_pages",
            "document exceeds the per-book page limit",
            status=413,
            detail={"max_pages": MAX_INGEST_PAGES, "pages": norm.page_count},
        )

    book_id = new_id()
    pdf_key = keys.pdf(book_id)
    # ``source_pdf_key`` always points at a real PDF (verbatim for a PDF upload,
    # the EPUB→PDF normalisation for an EPUB) so the resumable re-ingest path is
    # format-agnostic. The original EPUB + its cover are stored alongside.
    await anyio.to_thread.run_sync(
        container.object_store.put_bytes, pdf_key, norm.pdf_bytes, "application/pdf"
    )
    if norm.epub_bytes is not None:
        await anyio.to_thread.run_sync(
            container.object_store.put_bytes,
            keys.epub(book_id), norm.epub_bytes, EPUB_CONTENT_TYPE,
        )
    if norm.cover is not None:
        cover_bytes, cover_type = norm.cover
        await anyio.to_thread.run_sync(
            container.object_store.put_bytes, keys.cover(book_id), cover_bytes, cover_type
        )

    # Caller-supplied title/author win; otherwise fall back to document metadata
    # (EPUB), then the filename.
    resolved_title = _title_from(file.filename or "", title or norm.title)
    resolved_author = (author or norm.author or "").strip()[:512] or None

    async with container.session_factory() as session:
        book = await BookRepo(session).create(
            title=resolved_title,
            author=resolved_author,
            user_id=user.id,
            source_pdf_key=pdf_key,
            status=BookStatus.IMPORTING,
            art_direction=(art_direction.strip() if art_direction else None),
            book_id=book_id,
        )
        response = await _book_response(container, book)

    await container.redis.set_json(book_progress_key(book_id), {"stage": "importing", "pct": 0.0})

    # Phase A runs out-of-band; the response returns immediately (§9.1). The
    # normalised PDF bytes are what ingest extracts (the cover, if any, is read
    # from object storage by the ingest path and used as page 1's image).
    container.spawn(container.run_ingest(book_id, norm.pdf_bytes, None))

    logger.info(
        "books.uploaded",
        book_id=book_id,
        user_id=user.id,
        bytes=len(data),
        pages=norm.page_count,
        format="epub" if norm.epub_bytes is not None else "pdf",
    )
    return response


@router.get("", response_model=list[BookResponse])
async def list_books(container: ContainerDep, user: CurrentUser) -> list[BookResponse]:
    """List the books the current user owns (the shelf), newest first."""
    async with container.session_factory() as session:
        books = await BookRepo(session).list_for_user(user.id)
    return [await _book_response(container, b) for b in books]


@router.get("/{book_id}", response_model=BookResponse)
async def get_book(book_id: str, container: ContainerDep, user: CurrentUser) -> BookResponse:
    """Fetch a book with its import status + progress."""
    book = await _assert_owner(container, user, book_id)
    return await _book_response(container, book)


@router.get("/{book_id}/pages/{page_number}", response_model=PageResponse)
async def get_page(
    book_id: str, page_number: int, container: ContainerDep, user: CurrentUser
) -> PageResponse:
    """A page's presigned image URL, text, and per-word boxes (§9.4)."""
    await _assert_owner(container, user, book_id)
    async with container.session_factory() as session:
        page = await PageRepo(session).get_by_number(book_id, page_number)
    if page is None:
        raise APIError("page_not_found", "no such page", status=404)
    image_url = (
        container.object_store.presigned_get_url(page.image_key) if page.image_key else None
    )
    return PageResponse(
        book_id=book_id,
        page_number=page.page_number,
        image_url=image_url,
        text=page.text,
        word_boxes=list(page.word_boxes or []),
    )


@router.get("/{book_id}/canon", response_model=CanonResponse)
async def get_canon(book_id: str, container: ContainerDep, user: CurrentUser) -> CanonResponse:
    """The book's canon graph: the entity list the Director editor renders, plus
    the human-inspectable markdown vault export (§8.1)."""
    await _assert_owner(container, user, book_id)
    async with container.session_factory() as session:
        # Current (latest-version) entities — what the canon editor lists/edits.
        entities = await EntityRepo(session).list_active_at_beat(book_id, _LATEST_BEAT)
        items = [_canon_entity_response(container, e) for e in entities]
        # Continuity facts (§8.5) — active + retired, so "forgetting" is inspectable.
        states = await ContinuityStateRepo(session).list_for_book(book_id)
        state_items = [_canon_state_response(s) for s in states]
        # The markdown vault (§8.1) — joined into one document for inspection.
        export = await CanonVault(session, blob_store=container.object_store).export(book_id)
    markdown = "\n\n".join(export.files.values()) or None
    return CanonResponse(book_id=book_id, entities=items, states=state_items, markdown=markdown)


def _canon_state_response(state: ContinuityState) -> CanonStateResponse:
    """Project a continuity-state row for the canon editor (§8.5)."""
    return CanonStateResponse(
        id=state.id,
        subject_entity_key=state.subject_entity_key,
        predicate=state.predicate,
        object_value=state.object_value,
        valid_from_beat=state.valid_from_beat,
        valid_to_beat=state.valid_to_beat,
        version=state.version,
        active=state.valid_to_beat is None,
        source_span=state.source_span,
    )


def _canon_entity_response(container: Container, entity: Entity) -> CanonEntityResponse:
    """Project a canon entity row for the Director editor (presigned ref URLs)."""
    appearance: CanonAppearance | None = None
    raw_appearance = entity.appearance or {}
    if raw_appearance:
        appearance = CanonAppearance(
            description=raw_appearance.get("description"),
            reference_images=_canon_reference_images(container, raw_appearance),
        )
    return CanonEntityResponse(
        id=entity.entity_key,
        type=entity.type.value,
        name=entity.name,
        aliases=list(entity.aliases or []),
        description=entity.description,
        appearance=appearance,
        style_tokens=entity.style_tokens,
        voice=entity.voice,
        version=entity.version,
        valid_from_beat=entity.valid_from_beat,
        valid_to_beat=entity.valid_to_beat,
        first_appearance=entity.first_appearance,
    )


def _canon_reference_images(
    container: Container, appearance: dict[str, Any]
) -> list[CanonReferenceImage]:
    """Presign every locked reference image key into an ``oss_url`` (§8.1)."""
    images: list[CanonReferenceImage] = []
    raw = appearance.get("reference_images")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("oss_key")
            if isinstance(key, str):
                images.append(
                    CanonReferenceImage(
                        oss_url=container.object_store.presigned_get_url(key),
                        oss_key=key,
                        pose=item.get("pose"),
                        locked=bool(item.get("locked", False)),
                    )
                )
    ref_keys = appearance.get("reference_image_keys")
    if isinstance(ref_keys, list):
        locked = bool(appearance.get("locked", False))
        for key in ref_keys:
            if isinstance(key, str):
                images.append(
                    CanonReferenceImage(
                        oss_url=container.object_store.presigned_get_url(key),
                        oss_key=key,
                        locked=locked,
                    )
                )
    return images


@router.get("/{book_id}/shots", response_model=list[ShotResponse])
async def list_shots(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> list[ShotResponse]:
    """The book's shots (the §5.4 shot timeline) as a bare array."""
    await _assert_owner(container, user, book_id)
    async with container.session_factory() as session:
        stmt = (
            select(Shot)
            .where(Shot.book_id == book_id)
            .order_by(Shot.scene_id, Shot.beat_id, Shot.id)
        )
        rows = list((await session.execute(stmt)).scalars().all())
    return [_shot_response(container, row) for row in rows]


def _shot_response(container: Container, shot: Shot) -> ShotResponse:
    output: dict[str, Any] = shot.output or {}
    clip_key = output.get("clip_key")
    clip_url = output.get("clip_url") or (
        container.object_store.presigned_get_url(clip_key) if clip_key else None
    )
    return ShotResponse(
        shot_id=shot.id,
        beat_id=shot.beat_id,
        scene_id=shot.scene_id,
        source_span=shot.source_span,
        status=shot.status.value,
        render_mode=shot.render_mode,
        duration_s=shot.duration_s,
        qa=shot.qa,
        clip_url=clip_url,
        reference_image_ids=list(shot.reference_image_ids or []),
        clip_start_s=shot.clip_start_s,
        clip_end_s=shot.clip_end_s,
    )


__all__ = ["MAX_PDF_BYTES", "router"]
