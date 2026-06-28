"""Content-translation routes (app.translation, §8/§9).

The reader-facing translation surface, owned by the content-translation
subsystem. All routes are auth'd + ownership-checked against the durable
``books.user_id`` (fail-closed: a book that is not the caller's returns 404).

* ``GET  /translation/languages`` — the supported target languages (+ direction).
* ``POST /books/{id}/translate`` — translate a batch of source segments into a
  target language; persists the artifact + segments (cache-keyed, §8.7) and
  flags low-confidence segments for review. Returns the translated segments,
  cost accounting, and RTL flag.
* ``GET  /books/{id}/translations`` — list the book's translation artifacts.
* ``GET  /books/{id}/translations/{lang}/{kind}`` — fetch a persisted artifact's
  segments.
* ``GET/POST /books/{id}/glossary`` — read / add do-not-translate + glossary terms.
* ``GET  /books/{id}/reviews`` + ``POST /reviews/{id}/{action}`` — the human
  post-edit workflow (claim / approve / edit / reject / reopen).

These routes never render video and make no live model call in tests (the
provider is the injected :class:`FakeTranslationProvider`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.entity import Entity
from app.db.models.enums import EntityType
from app.db.repositories.book import BookRepo
from app.translation.artifacts import (
    ArtifactStatus,
    ReviewStatus,
    TranslationRepo,
)
from app.translation.canon import CanonName, glossary_from_canon_names, merge_glossaries
from app.translation.errors import TranslationError, UnknownLanguageError
from app.translation.glossary import Glossary, GlossaryEntry
from app.translation.hashing import source_content_hash, translation_key
from app.translation.languages import canonical_tag, get_language, supported_languages
from app.translation.memory_store import TranslationMemory
from app.translation.review import ReviewWorkflow
from app.translation.types import (
    ContentKind,
    Segment,
    TranslatedSegment,
    TranslationRequest,
)

logger = get_logger("app.api.translation")

router = APIRouter(tags=["translation"])


# --------------------------------------------------------------------------- #
# Schemas (local to the translation domain — additive, no shared-schema edits)
# --------------------------------------------------------------------------- #


class LanguageOut(BaseModel):
    tag: str
    name: str
    endonym: str
    direction: str
    rtl: bool


class SegmentIn(BaseModel):
    id: str
    text: str
    kind: Literal["page_text", "entity_description", "narration", "ui_fallback"] = "page_text"
    context: str | None = None


class TranslateRequestIn(BaseModel):
    target_lang: str = Field(..., description="BCP-47 target language tag (e.g. 'fr', 'zh-Hans').")
    segments: list[SegmentIn] = Field(..., min_length=1, max_length=500)
    source_lang: str | None = Field(None, description="Source tag; auto-detected if omitted.")
    back_translate: bool = False
    persist: bool = True
    use_memory: bool = True


class TranslatedSegmentOut(BaseModel):
    id: str
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    origin: str
    quality: float
    needs_review: bool
    warnings: list[str]


class CostOut(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    provider_calls: int
    cache_hits: int
    segments: int
    cache_hit_rate: float


class TranslateResponseOut(BaseModel):
    book_id: str
    source_lang: str
    target_lang: str
    rtl: bool
    review_count: int
    segments: list[TranslatedSegmentOut]
    cost: CostOut


class ArtifactOut(BaseModel):
    id: str
    target_lang: str
    content_kind: str
    source_lang: str
    status: str
    rtl: bool
    segment_count: int
    review_count: int
    glossary_version: int


class GlossaryEntryIn(BaseModel):
    source_term: str
    targets: dict[str, str] | None = None
    do_not_translate: bool = False
    case_sensitive: bool = False
    whole_word: bool = True


class GlossaryEntryOut(BaseModel):
    source_term: str
    targets: dict[str, str] | None
    do_not_translate: bool
    version: int
    source_kind: str


class ReviewOut(BaseModel):
    id: str
    segment_row_id: str
    status: str
    machine_text: str
    edited_text: str | None
    quality: float
    reason: str | None


class ReviewActionIn(BaseModel):
    edited_text: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _owned_book(container: Container, book_id: str, user_id: str) -> None:
    """404 unless ``book_id`` is the caller's (fail-closed ownership check)."""
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user_id:
        raise APIError("book_not_found", "no such book for this user", status=404)


async def _canon_character_names(container: Container, book_id: str) -> list[CanonName]:
    """Read the book's character entity names (latest version per key) for DNT.

    The canon (§8.1) is the source of proper nouns; locking them keeps "Elsa" as
    "Elsa" in every language. We take the highest version of each character
    ``entity_key`` and lift its name + aliases into do-not-translate terms.
    """
    async with container.session_factory() as session:
        stmt = (
            select(Entity)
            .where(Entity.book_id == book_id, Entity.type == EntityType.CHARACTER)
            .order_by(Entity.entity_key, Entity.version.desc())
            .distinct(Entity.entity_key)
        )
        rows = (await session.execute(stmt)).scalars().all()
    names: list[CanonName] = []
    for row in rows:
        aliases = tuple(a for a in (row.aliases or []) if isinstance(a, str) and a.strip())
        names.append(CanonName(entity_key=row.entity_key, name=row.name, aliases=aliases))
    return names


async def _build_glossary(container: Container, book_id: str) -> Glossary:
    """Build the book's glossary: canon character DNT terms + persisted entries."""
    async with container.session_factory() as session:
        rows = await TranslationRepo(session).list_glossary(book_id)
    persisted_entries = [
        GlossaryEntry(
            source=row.source_term,
            targets=dict(row.targets) if row.targets else {},
            do_not_translate=row.do_not_translate,
            case_sensitive=row.case_sensitive,
            whole_word=row.whole_word,
        )
        for row in rows
    ]
    persisted_version = max((row.version for row in rows), default=0)
    persisted = Glossary(persisted_entries, version=persisted_version)

    canon_names = await _canon_character_names(container, book_id)
    if not canon_names:
        return persisted
    canon = glossary_from_canon_names(canon_names, version=persisted_version or 1)
    # Persisted entries win on a source collision (an explicit forced target
    # overrides a canon DNT lock for the same surface form).
    return merge_glossaries(canon, persisted)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/translation/languages")
async def list_languages(user: CurrentUser) -> dict[str, list[LanguageOut]]:
    """The supported target languages, with writing direction."""
    return {
        "languages": [
            LanguageOut(
                tag=lang.tag,
                name=lang.name,
                endonym=lang.endonym,
                direction=lang.direction.value,
                rtl=lang.is_rtl,
            )
            for lang in supported_languages()
        ]
    }


@router.post("/books/{book_id}/translate", dependencies=[Depends(write_rate_limit)])
async def translate_book_content(
    book_id: str,
    body: TranslateRequestIn,
    container: ContainerDep,
    user: CurrentUser,
) -> TranslateResponseOut:
    """Translate a batch of source segments into a target language (§8/§9).

    Persists the artifact + segments keyed to source-content hashes so a re-read
    is free; low-confidence segments are flagged for human post-edit.
    """
    await _owned_book(container, book_id, user.id)
    try:
        target = canonical_tag(body.target_lang)
        get_language(target)
    except UnknownLanguageError as exc:
        raise APIError("unknown_language", str(exc), status=400) from exc

    glossary = await _build_glossary(container, book_id)
    memory = TranslationMemory(fuzzy_threshold=container.settings.translation_fuzzy_threshold)

    # Hydrate the in-process TM from any prior persisted segments (the §8.7 cache).
    async with container.session_factory() as session:
        await TranslationRepo(session).hydrate_memory(memory, book_id=book_id, target_lang=target)

    service = container.build_translation_service(glossary=glossary, memory=memory)

    segments = tuple(
        Segment(id=s.id, text=s.text, kind=ContentKind(s.kind), context=s.context)
        for s in body.segments
    )
    request = TranslationRequest(
        book_id=book_id,
        target_lang=target,
        segments=segments,
        source_lang=body.source_lang,
        back_translate=body.back_translate,
        use_memory=body.use_memory,
        persist=body.persist,
    )
    try:
        result = await service.translate(request)
    except TranslationError as exc:
        raise APIError(exc.code, exc.message, status=400) from exc

    if body.persist:
        kind_by_id = {s.id: s.kind for s in body.segments}
        await _persist_result(
            container, book_id, result, kind_by_id=kind_by_id, glossary_version=glossary.version
        )

    return _to_response(result)


async def _persist_result(
    container: Container,
    book_id: str,
    result: Any,
    *,
    kind_by_id: Mapping[str, str],
    glossary_version: int,
) -> None:
    """Write the artifact + segments + review rows, grouped by content kind."""
    # One artifact per content kind (a page-text translation and a narration
    # translation of the same book are distinct artifacts).
    by_kind: dict[str, list[TranslatedSegment]] = {}
    for seg in result.segments:
        kind = kind_by_id.get(seg.id, ContentKind.PAGE_TEXT.value)
        by_kind.setdefault(kind, []).append(seg)

    async with container.session_factory() as session:
        repo = TranslationRepo(session)
        for kind, segs in by_kind.items():
            review_count = sum(1 for s in segs if s.needs_review)
            status = ArtifactStatus.NEEDS_REVIEW if review_count else ArtifactStatus.READY
            artifact = await repo.upsert_artifact(
                book_id=book_id,
                source_lang=result.source_lang,
                target_lang=result.target_lang,
                content_kind=kind,
                glossary_version=glossary_version,
                rtl=result.rtl,
                status=status,
                segment_count=len(segs),
                review_count=review_count,
                cost={
                    "input_tokens": result.cost.input_tokens,
                    "output_tokens": result.cost.output_tokens,
                    "provider_calls": result.cost.provider_calls,
                    "cache_hits": result.cost.cache_hits,
                    "segments": result.cost.segments,
                },
            )
            for seg in segs:
                key_hash = translation_key(
                    source_text=seg.source_text,
                    source_lang=seg.source_lang,
                    target_lang=seg.target_lang,
                    content_kind=kind,
                    glossary_version=glossary_version,
                )
                row = await repo.upsert_segment(
                    artifact_id=artifact.id,
                    book_id=book_id,
                    translated=seg,
                    content_kind=kind,
                    source_hash=source_content_hash(seg.source_text),
                    translation_key_hash=key_hash,
                    glossary_version=glossary_version,
                )
                if seg.needs_review:
                    await repo.create_review(
                        book_id=book_id,
                        segment_row_id=row.id,
                        machine_text=seg.translated_text,
                        quality=seg.quality,
                        reason="; ".join(seg.warnings) or None,
                    )


def _to_response(result: Any) -> TranslateResponseOut:
    return TranslateResponseOut(
        book_id=result.book_id,
        source_lang=result.source_lang,
        target_lang=result.target_lang,
        rtl=result.rtl,
        review_count=result.review_count,
        segments=[
            TranslatedSegmentOut(
                id=s.id,
                source_text=s.source_text,
                translated_text=s.translated_text,
                source_lang=s.source_lang,
                target_lang=s.target_lang,
                origin=s.origin.value,
                quality=s.quality,
                needs_review=s.needs_review,
                warnings=list(s.warnings),
            )
            for s in result.segments
        ],
        cost=CostOut(
            input_tokens=result.cost.input_tokens,
            output_tokens=result.cost.output_tokens,
            total_tokens=result.cost.total_tokens,
            provider_calls=result.cost.provider_calls,
            cache_hits=result.cost.cache_hits,
            segments=result.cost.segments,
            cache_hit_rate=round(result.cost.cache_hit_rate, 4),
        ),
    )


@router.get("/books/{book_id}/translations")
async def list_translations(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> dict[str, list[ArtifactOut]]:
    """List the persisted translation artifacts for a book."""
    await _owned_book(container, book_id, user.id)
    async with container.session_factory() as session:
        artifacts = await TranslationRepo(session).list_artifacts(book_id)
    return {
        "artifacts": [
            ArtifactOut(
                id=a.id,
                target_lang=a.target_lang,
                content_kind=a.content_kind,
                source_lang=a.source_lang,
                status=a.status.value,
                rtl=a.rtl,
                segment_count=a.segment_count,
                review_count=a.review_count,
                glossary_version=a.glossary_version,
            )
            for a in artifacts
        ]
    }


@router.get("/books/{book_id}/translations/{target_lang}/{content_kind}")
async def get_translation(
    book_id: str,
    target_lang: str,
    content_kind: str,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Fetch a persisted artifact and its translated segments."""
    await _owned_book(container, book_id, user.id)
    try:
        target = canonical_tag(target_lang)
    except UnknownLanguageError as exc:
        raise APIError("unknown_language", str(exc), status=400) from exc
    async with container.session_factory() as session:
        repo = TranslationRepo(session)
        artifact = await repo.get_artifact(
            book_id=book_id, target_lang=target, content_kind=content_kind
        )
        if artifact is None:
            raise APIError("translation_not_found", "no translation for this book", status=404)
        segments = await repo.list_segments(artifact.id)
    return {
        "artifact": ArtifactOut(
            id=artifact.id,
            target_lang=artifact.target_lang,
            content_kind=artifact.content_kind,
            source_lang=artifact.source_lang,
            status=artifact.status.value,
            rtl=artifact.rtl,
            segment_count=artifact.segment_count,
            review_count=artifact.review_count,
            glossary_version=artifact.glossary_version,
        ).model_dump(),
        "segments": [
            {
                "segment_id": s.segment_id,
                "source_text": s.source_text,
                "translated_text": s.translated_text,
                "origin": s.origin,
                "quality": s.quality,
                "needs_review": s.needs_review,
            }
            for s in segments
        ],
    }


@router.get("/books/{book_id}/glossary")
async def get_glossary(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> dict[str, list[GlossaryEntryOut]]:
    """Read a book's glossary / do-not-translate terms."""
    await _owned_book(container, book_id, user.id)
    async with container.session_factory() as session:
        rows = await TranslationRepo(session).list_glossary(book_id)
    return {
        "entries": [
            GlossaryEntryOut(
                source_term=r.source_term,
                targets=dict(r.targets) if r.targets else None,
                do_not_translate=r.do_not_translate,
                version=r.version,
                source_kind=r.source_kind,
            )
            for r in rows
        ]
    }


@router.post("/books/{book_id}/glossary", dependencies=[Depends(write_rate_limit)])
async def add_glossary_entry(
    book_id: str,
    body: GlossaryEntryIn,
    container: ContainerDep,
    user: CurrentUser,
) -> GlossaryEntryOut:
    """Add or update a glossary / do-not-translate term for a book."""
    await _owned_book(container, book_id, user.id)
    if not body.do_not_translate and not body.targets:
        raise APIError(
            "glossary_error", "entry must be do_not_translate or carry targets", status=400
        )
    async with container.session_factory() as session:
        row = await TranslationRepo(session).upsert_glossary_entry(
            book_id=book_id,
            source_term=body.source_term,
            targets=body.targets,
            do_not_translate=body.do_not_translate,
            case_sensitive=body.case_sensitive,
            whole_word=body.whole_word,
        )
        await session.commit()
    return GlossaryEntryOut(
        source_term=row.source_term,
        targets=dict(row.targets) if row.targets else None,
        do_not_translate=row.do_not_translate,
        version=row.version,
        source_kind=row.source_kind,
    )


@router.get("/books/{book_id}/reviews")
async def list_reviews(
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    status: str | None = None,
) -> dict[str, list[ReviewOut]]:
    """List the human post-edit reviews for a book (optionally filtered by state)."""
    await _owned_book(container, book_id, user.id)
    status_enum: ReviewStatus | None = None
    if status is not None:
        try:
            status_enum = ReviewStatus(status)
        except ValueError as exc:
            raise APIError("bad_status", f"unknown review status {status!r}", status=400) from exc
    async with container.session_factory() as session:
        reviews = await TranslationRepo(session).list_reviews(book_id, status=status_enum)
    return {"reviews": [_review_out(r) for r in reviews]}


@router.post("/reviews/{review_id}/{action}", dependencies=[Depends(write_rate_limit)])
async def act_on_review(
    review_id: str,
    action: Literal["claim", "approve", "edit", "reject", "reopen"],
    body: ReviewActionIn,
    container: ContainerDep,
    user: CurrentUser,
) -> ReviewOut:
    """Drive the review state machine (claim/approve/edit/reject/reopen)."""
    async with container.session_factory() as session:
        repo = TranslationRepo(session)
        review = await repo.get_review(review_id)
        if review is None:
            raise APIError("review_not_found", "no such review", status=404)
        await _owned_book(container, review.book_id, user.id)
        workflow = ReviewWorkflow(repo)
        try:
            if action == "claim":
                review = await workflow.claim(review_id, reviewer_id=user.id)
            elif action == "approve":
                review = await workflow.approve(review_id, reviewer_id=user.id)
            elif action == "edit":
                if not body.edited_text:
                    raise APIError("bad_request", "edit requires edited_text", status=400)
                review = await workflow.edit(
                    review_id, edited_text=body.edited_text, reviewer_id=user.id
                )
            elif action == "reject":
                review = await workflow.reject(
                    review_id, reason=body.reason, reviewer_id=user.id
                )
            else:  # reopen
                review = await workflow.reopen(review_id)
        except TranslationError as exc:
            raise APIError(exc.code, exc.message, status=409) from exc
        await session.commit()
    return _review_out(review)


def _review_out(review: Any) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        segment_row_id=review.segment_row_id,
        status=review.status.value,
        machine_text=review.machine_text,
        edited_text=review.edited_text,
        quality=review.quality,
        reason=review.reason,
    )


__all__ = ["router"]
