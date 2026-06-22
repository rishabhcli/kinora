"""Book routes — upload + ingest trigger, shelf, pages, canon, shots (§5.1/§9.1).

``POST /books`` validates the PDF (content-type, ``%PDF`` magic, size cap,
sanitized filename), stores it to object storage under ``pdfs/``, creates the
``importing`` book row, records ownership, and triggers **Phase A** ingest
out-of-band as a tracked background task whose progress callback publishes
events to the book's Redis channel (the shelf progress strip). The read routes
project the imported artifacts: the shelf, a page (presigned image + text +
word boxes for karaoke), the human-inspectable canon vault, and the shot
timeline.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
    PageResponse,
    ShotResponse,
)
from app.composition import Container
from app.core.logging import get_logger
from app.db.base import new_id
from app.db.models.book import Book
from app.db.models.entity import Entity
from app.db.models.enums import BookStatus
from app.db.models.shot import Shot
from app.db.models.user import User
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.entity import EntityRepo
from app.memory.canon_vault import CanonVault
from app.storage.object_store import keys

logger = get_logger("app.api.books")

router = APIRouter(prefix="/books", tags=["books"])

#: Hard upload size cap — generous for an illustrated PDF, bounded for safety.
MAX_PDF_BYTES = 50 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"
_ALLOWED_CONTENT_TYPES = frozenset(
    {"application/pdf", "application/octet-stream", "binary/octet-stream", ""}
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Resolve canon "as of the latest version" — a beat beyond any real one, so the
#: still-open (current) version of every entity is returned.
_LATEST_BEAT = 2**31 - 1


def _user_books_key(user_id: str) -> str:
    return f"kinora:user:{user_id}:books"


def _progress_key(book_id: str) -> str:
    return f"kinora:book:progress:{book_id}"


def _sanitize_filename(name: str | None) -> str:
    """Strip any path and unsafe characters from an uploaded filename."""
    base = os.path.basename(name or "").strip()
    safe = "".join(c for c in base if c.isalnum() or c in (" ", ".", "_", "-")).strip()
    return safe or "untitled.pdf"


def _title_from(filename: str, given: str | None) -> str:
    if given and given.strip():
        return given.strip()[:512]
    stem = os.path.splitext(_sanitize_filename(filename))[0]
    return (stem or "Untitled").replace("_", " ")[:512]


async def _assert_owner(container: Container, user: User, book_id: str) -> Book:
    """Load a book the user owns, or 404 (ownership is tracked in Redis)."""
    owned = await container.redis.raw.sismember(_user_books_key(user.id), book_id)
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or not owned:
        raise APIError("book_not_found", "no such book for this user", status=404)
    return book


async def _book_response(container: Container, book: Book) -> BookResponse:
    progress = await container.redis.get_json(_progress_key(book.id))
    pct: float | None = None
    stage: str | None = None
    if isinstance(progress, dict):
        pct = progress.get("pct")
        stage = progress.get("stage")
    elif book.status is BookStatus.READY:
        pct = 1.0
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
    )


@router.post("", response_model=BookResponse, status_code=status.HTTP_201_CREATED)
async def upload_book(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="The source PDF")],
    title: Annotated[str | None, Form()] = None,
    author: Annotated[str | None, Form()] = None,
    art_direction: Annotated[str | None, Form()] = None,
) -> BookResponse:
    """Validate + store a PDF, create the book, and trigger Phase A ingest.

    Returns the freshly-created book (status ``importing``) directly — the shelf
    prepends it and polls until it is ``ready``."""
    if file.size is not None and file.size > MAX_PDF_BYTES:
        raise APIError(
            "file_too_large", "PDF exceeds the size limit", status=413,
            detail={"max_bytes": MAX_PDF_BYTES},
        )
    if file.content_type is not None and file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise APIError(
            "unsupported_media_type", "expected a PDF upload", status=415,
            detail={"content_type": file.content_type},
        )

    data = await file.read()
    if len(data) > MAX_PDF_BYTES:
        raise APIError(
            "file_too_large", "PDF exceeds the size limit", status=413,
            detail={"max_bytes": MAX_PDF_BYTES},
        )
    if not data[:1024].lstrip().startswith(_PDF_MAGIC):
        raise APIError("invalid_pdf", "file is not a valid PDF (missing %PDF header)", status=400)

    book_id = new_id()
    pdf_key = keys.pdf(book_id)
    await anyio.to_thread.run_sync(
        container.object_store.put_bytes, pdf_key, data, "application/pdf"
    )

    async with container.session_factory() as session:
        book = await BookRepo(session).create(
            title=_title_from(file.filename or "", title),
            author=(author.strip()[:512] if author else None),
            source_pdf_key=pdf_key,
            status=BookStatus.IMPORTING,
            art_direction=(art_direction.strip() if art_direction else None),
            book_id=book_id,
        )
        response = await _book_response(container, book)

    await container.redis.raw.sadd(_user_books_key(user.id), book_id)
    await container.redis.set_json(_progress_key(book_id), {"stage": "importing", "pct": 0.0})

    # Phase A runs out-of-band; the response returns immediately (§9.1).
    container.spawn(container.run_ingest(book_id, data, None))

    logger.info("books.uploaded", book_id=book_id, user_id=user.id, bytes=len(data))
    return response


@router.get("", response_model=list[BookResponse])
async def list_books(container: ContainerDep, user: CurrentUser) -> list[BookResponse]:
    """List the books the current user has uploaded (the shelf), newest first."""
    owned = await container.redis.raw.smembers(_user_books_key(user.id))
    async with container.session_factory() as session:
        repo = BookRepo(session)
        books = [b for b in [await repo.get(bid) for bid in owned] if b is not None]
    books.sort(key=lambda b: b.created_at or _EPOCH, reverse=True)
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
        # The markdown vault (§8.1) — joined into one document for inspection.
        export = await CanonVault(session, blob_store=container.object_store).export(book_id)
    markdown = "\n\n".join(export.files.values()) or None
    return CanonResponse(book_id=book_id, entities=items, markdown=markdown)


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
                        oss_url=container.object_store.presigned_get_url(key), locked=locked
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
    )


__all__ = ["MAX_PDF_BYTES", "router"]
