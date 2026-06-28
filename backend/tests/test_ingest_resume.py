"""Resumability + incremental re-ingest at the service level (§9.1) — DB-backed.

Verifies that a full ingest records every milestone checkpoint, that a forced
re-ingest clears + re-records them, and that :func:`plan_reingest` correctly
diffs a changed source against the persisted pages. All heavy provider calls are
faked (no network); SKIPs when ``KINORA_TEST_DATABASE_URL`` is unset.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete

from app.db.models.book import Book
from app.db.models.ingest_checkpoint import IngestMilestone
from app.db.repositories.book import BookRepo
from app.db.repositories.ingest_checkpoint import IngestCheckpointRepo
from app.ingest.service import IngestOptions, ingest_pdf, plan_reingest
from app.providers import Providers
from tests.test_ingest_support import (
    MemoryBlobStore,
    build_test_pdf,
    committing_session_factory,
    create_schema,
    new_engine,
    one_hot,
    providers,  # noqa: F401  (pytest fixture)
    requires_db,
)

pytestmark = requires_db

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-keyframe"
_PAGES = [
    "The little fox runs quickly through the dark green forest at sunrise.",
    "The wise owl watches the quiet world from a tall oak tree at dusk.",
]
_VL_REPLY: dict[str, Any] = {
    "summary": "A fox and an owl.",
    "described_visuals": "a red fox and a grey owl",
    "entities": [
        {"name": "Fox", "kind": "character", "appearance": "a small red fox"},
        {"name": "Owl", "kind": "character", "appearance": "a grey owl"},
    ],
    "states": [{"subject": "Fox", "predicate": "located_in", "object": "forest"}],
    "illustrations": [],
}
_CHAT_REPLY: dict[str, Any] = {
    "beats": [
        {
            "summary": "The fox runs through the forest",
            "entities": ["Fox"],
            "described_visuals": "a fox running",
            "mood": "lively",
            "source_span": {"word_range": [0, 0]},
        },
        {
            "summary": "The owl watches",
            "entities": ["Owl"],
            "described_visuals": "an owl perched",
            "mood": "calm",
            "source_span": {"word_range": [0, 0]},
        },
    ]
}


def _patch_providers(providers: Providers, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    async def fake_vl(images: list[Any], prompt: str, **kwargs: Any) -> dict[str, Any]:
        return _VL_REPLY

    async def fake_chat_json(messages: Any, model: str, **kwargs: Any) -> dict[str, Any]:
        return _CHAT_REPLY

    async def fake_generate(prompt: str, **kwargs: Any) -> list[bytes]:
        return [_PNG]

    async def fake_embed(images: list[bytes]) -> list[list[float]]:
        return [one_hot(b) for b in images]

    monkeypatch.setattr(providers.vl, "analyze_json", fake_vl)
    monkeypatch.setattr(providers.chat, "chat_json", fake_chat_json)
    monkeypatch.setattr(providers.image, "generate", fake_generate)
    monkeypatch.setattr(providers.embeddings, "embed_images", fake_embed)


async def test_ingest_records_all_milestones(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    await create_schema(engine)
    factory = committing_session_factory(engine)
    store = MemoryBlobStore()
    _patch_providers(providers, monkeypatch)

    book_id = ""
    try:
        async with factory() as s:
            book = await BookRepo(s).create(title="Resumable", art_direction="storybook")
            book_id = book.id

        await ingest_pdf(
            book_id,
            build_test_pdf(_PAGES),
            providers=providers,
            blob_store=store,
            session_factory=factory,
            options=IngestOptions(min_beats=2),
        )

        async with factory() as s:
            done = await IngestCheckpointRepo(s).completed(book_id)
        assert done == {
            IngestMilestone.EXTRACT,
            IngestMilestone.ANALYZE,
            IngestMilestone.CANON,
            IngestMilestone.SHOT_PLAN,
            IngestMilestone.IDENTITY_LOCK,
        }
    finally:
        if book_id:
            async with factory() as s:
                await s.execute(delete(Book).where(Book.id == book_id))
        await engine.dispose()


async def test_forced_reingest_clears_and_rerecords(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    await create_schema(engine)
    factory = committing_session_factory(engine)
    store = MemoryBlobStore()
    _patch_providers(providers, monkeypatch)

    book_id = ""
    try:
        async with factory() as s:
            book = await BookRepo(s).create(title="Forced", art_direction="storybook")
            book_id = book.id

        pdf = build_test_pdf(_PAGES)
        await ingest_pdf(
            book_id, pdf, providers=providers, blob_store=store,
            session_factory=factory, options=IngestOptions(min_beats=2),
        )
        # A forced re-ingest of the now-ready book must succeed (clears checkpoints,
        # re-runs every stage, ends ready) without colliding on PKs.
        result = await ingest_pdf(
            book_id, pdf, providers=providers, blob_store=store,
            session_factory=factory, options=IngestOptions(min_beats=2, force=True),
        )
        assert result.status == "ready"
        async with factory() as s:
            done = await IngestCheckpointRepo(s).completed(book_id)
        assert IngestMilestone.IDENTITY_LOCK in done
    finally:
        if book_id:
            async with factory() as s:
                await s.execute(delete(Book).where(Book.id == book_id))
        await engine.dispose()


async def test_plan_reingest_detects_changed_pages(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    await create_schema(engine)
    factory = committing_session_factory(engine)
    store = MemoryBlobStore()
    _patch_providers(providers, monkeypatch)

    book_id = ""
    try:
        async with factory() as s:
            book = await BookRepo(s).create(title="Diffable", art_direction="storybook")
            book_id = book.id
        await ingest_pdf(
            book_id, build_test_pdf(_PAGES), providers=providers, blob_store=store,
            session_factory=factory, options=IngestOptions(min_beats=2),
        )

        # Identical source -> no change.
        same = await plan_reingest(book_id, build_test_pdf(_PAGES), session_factory=factory)
        assert same.identical is True
        assert same.pages_to_reanalyze == []

        # One page edited -> exactly that page flagged for re-analysis.
        edited = list(_PAGES)
        edited[1] = "The wise owl flies away into a brand new starry midnight sky now."
        plan = await plan_reingest(book_id, build_test_pdf(edited), session_factory=factory)
        assert plan.identical is False
        assert plan.num_changed == 1
        assert plan.pages_to_reanalyze == [2]
    finally:
        if book_id:
            async with factory() as s:
                await s.execute(delete(Book).where(Book.id == book_id))
        await engine.dispose()
