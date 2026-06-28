"""DB-bound tests for translation persistence + the review workflow.

These run against an isolated Postgres DB (``kinora_translation_test`` on :5433
by default; override with ``KINORA_TRANSLATION_TEST_DATABASE_URL``) and **skip
cleanly** when it is unreachable, so the unit suite still runs anywhere. Each
test gets a clean schema (create_all + truncate). No live model calls — the
service uses the :class:`FakeTranslationProvider`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base, new_id
from app.db.models.book import Book
from app.db.models.enums import BookStatus
from app.translation.artifacts import (
    ArtifactStatus,
    ReviewStatus,
    TranslationRepo,
)
from app.translation.errors import ReviewStateError
from app.translation.hashing import source_content_hash, translation_key
from app.translation.memory_store import TranslationMemory
from app.translation.review import ReviewWorkflow, assert_transition
from app.translation.types import ContentKind, TranslatedSegment, TranslationOrigin

# ``asyncio_mode = "auto"`` runs the async tests; this module also has sync tests
# (the state-machine + transition checks), so no module-level asyncio mark.

_DEFAULT_URL = "postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_translation_test"
_DB_URL = os.environ.get("KINORA_TRANSLATION_TEST_DATABASE_URL", _DEFAULT_URL)


async def _reachable(url: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any connection failure → skip cleanly
        return False
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    if not await _reachable(_DB_URL):
        pytest.skip(f"translation DB not reachable at {_DB_URL}")
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_book(session: AsyncSession, *, book_id: str | None = None) -> str:
    bid = book_id or new_id()
    session.add(Book(id=bid, title="Test Book", status=BookStatus.READY))
    await session.flush()
    return bid


def _seg(seg_id: str, *, review: bool = False, quality: float = 0.95) -> TranslatedSegment:
    return TranslatedSegment(
        id=seg_id,
        source_text=f"source {seg_id}",
        translated_text=f"target {seg_id}",
        source_lang="en",
        target_lang="fr",
        origin=TranslationOrigin.PROVIDER,
        quality=quality,
        needs_review=review,
        warnings=("low quality",) if review else (),
    )


# -- artifact + segment persistence ----------------------------------------- #


async def test_upsert_artifact_and_segment(session: AsyncSession) -> None:
    book_id = await _make_book(session)
    repo = TranslationRepo(session)
    artifact = await repo.upsert_artifact(
        book_id=book_id,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
        rtl=False,
        status=ArtifactStatus.READY,
        segment_count=1,
        review_count=0,
        cost={"input_tokens": 10},
    )
    seg = _seg("s0")
    key = translation_key(
        source_text=seg.source_text,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    row = await repo.upsert_segment(
        artifact_id=artifact.id,
        book_id=book_id,
        translated=seg,
        content_kind=ContentKind.PAGE_TEXT,
        source_hash=source_content_hash(seg.source_text),
        translation_key_hash=key,
        glossary_version=0,
    )
    assert row.translated_text == "target s0"
    fetched = await repo.get_segment_by_key(
        book_id=book_id, target_lang="fr", translation_key_hash=key
    )
    assert fetched is not None and fetched.id == row.id


async def test_upsert_artifact_is_idempotent(session: AsyncSession) -> None:
    book_id = await _make_book(session)
    repo = TranslationRepo(session)
    a1 = await repo.upsert_artifact(
        book_id=book_id,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
        rtl=False,
        status=ArtifactStatus.DRAFT,
        segment_count=0,
        review_count=0,
        cost=None,
    )
    a2 = await repo.upsert_artifact(
        book_id=book_id,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=1,
        rtl=False,
        status=ArtifactStatus.READY,
        segment_count=3,
        review_count=0,
        cost=None,
    )
    assert a1.id == a2.id
    assert a2.status is ArtifactStatus.READY
    assert a2.segment_count == 3
    artifacts = await repo.list_artifacts(book_id)
    assert len(artifacts) == 1


async def test_hydrate_memory_round_trips(session: AsyncSession) -> None:
    book_id = await _make_book(session)
    repo = TranslationRepo(session)
    artifact = await repo.upsert_artifact(
        book_id=book_id,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
        rtl=False,
        status=ArtifactStatus.READY,
        segment_count=1,
        review_count=0,
        cost=None,
    )
    seg = _seg("s0")
    await repo.upsert_segment(
        artifact_id=artifact.id,
        book_id=book_id,
        translated=seg,
        content_kind=ContentKind.PAGE_TEXT,
        source_hash=source_content_hash(seg.source_text),
        translation_key_hash=translation_key(
            source_text=seg.source_text,
            source_lang="en",
            target_lang="fr",
            content_kind=ContentKind.PAGE_TEXT,
            glossary_version=0,
        ),
        glossary_version=0,
    )
    await session.commit()

    mem = TranslationMemory()
    count = await repo.hydrate_memory(mem, book_id=book_id, target_lang="fr")
    assert count == 1
    hit = mem.get_exact(
        source_text="source s0",
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert hit is not None and hit.translated_text == "target s0"


# -- glossary persistence ---------------------------------------------------- #


async def test_glossary_upsert_bumps_version(session: AsyncSession) -> None:
    book_id = await _make_book(session)
    repo = TranslationRepo(session)
    r1 = await repo.upsert_glossary_entry(
        book_id=book_id, source_term="Elsa", do_not_translate=True
    )
    assert r1.version == 1
    r2 = await repo.upsert_glossary_entry(
        book_id=book_id, source_term="Elsa", targets={"fr": "Elsa"}
    )
    assert r2.version == 2
    rows = await repo.list_glossary(book_id)
    assert len(rows) == 1


# -- review workflow --------------------------------------------------------- #


async def test_review_state_machine_transitions() -> None:
    assert_transition(ReviewStatus.PENDING, ReviewStatus.IN_REVIEW)
    assert_transition(ReviewStatus.IN_REVIEW, ReviewStatus.EDITED)
    with pytest.raises(ReviewStateError):
        assert_transition(ReviewStatus.APPROVED, ReviewStatus.IN_REVIEW)


async def _seed_review(session: AsyncSession) -> tuple[str, str, TranslationRepo]:
    book_id = await _make_book(session)
    repo = TranslationRepo(session)
    artifact = await repo.upsert_artifact(
        book_id=book_id,
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
        rtl=False,
        status=ArtifactStatus.NEEDS_REVIEW,
        segment_count=1,
        review_count=1,
        cost=None,
    )
    seg = _seg("s0", review=True, quality=0.4)
    row = await repo.upsert_segment(
        artifact_id=artifact.id,
        book_id=book_id,
        translated=seg,
        content_kind=ContentKind.PAGE_TEXT,
        source_hash=source_content_hash(seg.source_text),
        translation_key_hash=translation_key(
            source_text=seg.source_text,
            source_lang="en",
            target_lang="fr",
            content_kind=ContentKind.PAGE_TEXT,
            glossary_version=0,
        ),
        glossary_version=0,
    )
    review = await repo.create_review(
        book_id=book_id,
        segment_row_id=row.id,
        machine_text=seg.translated_text,
        quality=seg.quality,
        reason="low quality",
    )
    return book_id, review.id, repo


async def test_review_claim_and_edit_updates_segment_and_memory(session: AsyncSession) -> None:
    book_id, review_id, repo = await _seed_review(session)
    mem = TranslationMemory()
    wf = ReviewWorkflow(repo, memory=mem)

    claimed = await wf.claim(review_id, reviewer_id="u1")
    assert claimed.status is ReviewStatus.IN_REVIEW

    edited = await wf.edit(review_id, edited_text="texte corrigé", reviewer_id="u1")
    assert edited.status is ReviewStatus.EDITED
    assert edited.edited_text == "texte corrigé"

    # The segment now carries the human edit, origin POST_EDIT, flag cleared.
    segs = await repo.list_segments((await repo.list_artifacts(book_id))[0].id)
    assert segs[0].translated_text == "texte corrigé"
    assert segs[0].origin == TranslationOrigin.POST_EDIT.value
    assert segs[0].needs_review is False

    # And the edit is in the TM for free reuse.
    hit = mem.get_exact(
        source_text="source s0",
        source_lang="en",
        target_lang="fr",
        content_kind=ContentKind.PAGE_TEXT,
        glossary_version=0,
    )
    assert hit is not None and hit.translated_text == "texte corrigé"


async def test_review_approve_clears_flag(session: AsyncSession) -> None:
    book_id, review_id, repo = await _seed_review(session)
    wf = ReviewWorkflow(repo)
    approved = await wf.approve(review_id, reviewer_id="u1")
    assert approved.status is ReviewStatus.APPROVED
    segs = await repo.list_segments((await repo.list_artifacts(book_id))[0].id)
    assert segs[0].needs_review is False


async def test_review_reject_and_reopen(session: AsyncSession) -> None:
    _, review_id, repo = await _seed_review(session)
    wf = ReviewWorkflow(repo)
    rejected = await wf.reject(review_id, reason="wrong tone", reviewer_id="u1")
    assert rejected.status is ReviewStatus.REJECTED
    reopened = await wf.reopen(review_id)
    assert reopened.status is ReviewStatus.PENDING


async def test_review_summary_counts(session: AsyncSession) -> None:
    book_id, review_id, repo = await _seed_review(session)
    wf = ReviewWorkflow(repo)
    summary = await wf.summary(book_id)
    assert summary.pending == 1
    assert summary.open == 1
    await wf.approve(review_id)
    summary2 = await wf.summary(book_id)
    assert summary2.approved == 1
    assert summary2.open == 0


async def test_edit_empty_text_rejected(session: AsyncSession) -> None:
    _, review_id, repo = await _seed_review(session)
    wf = ReviewWorkflow(repo)
    with pytest.raises(ReviewStateError):
        await wf.edit(review_id, edited_text="   ")
