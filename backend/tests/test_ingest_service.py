"""Full Phase A orchestration: importing → ready, progress events, all heavy calls faked."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete

from app.db.models.book import Book
from app.db.models.enums import BookStatus
from app.db.repositories.book import BookRepo
from app.db.repositories.shot import SourceSpanRepo
from app.ingest.pdf_extract import page_image_key
from app.ingest.service import IngestOptions, ingest_pdf
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
    "summary": "A fox and an owl in the forest.",
    "described_visuals": "a red fox and a grey owl among trees",
    "entities": [
        {"name": "Fox", "kind": "character", "appearance": "a small bright red fox"},
        {"name": "Owl", "kind": "character", "appearance": "a wise old grey owl"},
    ],
    "states": [{"subject": "Fox", "predicate": "located_in", "object": "forest"}],
    "illustrations": [{"description": "a coloured circle", "kind": "illustration"}],
}

_CHAT_REPLY: dict[str, Any] = {
    "beats": [
        {
            "summary": "The fox runs through the forest",
            "entities": ["Fox"],
            "described_visuals": "a fox running among trees",
            "mood": "lively",
            "source_span": {"word_range": [0, 0]},
        },
        {
            "summary": "The owl watches from a tree",
            "entities": ["Owl"],
            "described_visuals": "an owl perched in a tree",
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


async def test_full_ingest_importing_to_ready_with_progress(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = new_engine()
    await create_schema(engine)
    factory = committing_session_factory(engine)
    store = MemoryBlobStore()
    _patch_providers(providers, monkeypatch)

    events: list[tuple[str, float]] = []

    async def progress(stage: str, pct: float) -> None:
        events.append((stage, pct))

    book_id = ""
    try:
        async with factory() as s:
            book = await BookRepo(s).create(title="Smoke", art_direction="painterly storybook")
            book_id = book.id
            assert book.status == BookStatus.IMPORTING

        result = await ingest_pdf(
            book_id,
            build_test_pdf(_PAGES),
            providers=providers,
            blob_store=store,
            session_factory=factory,
            progress=progress,
            options=IngestOptions(min_beats=2),
        )

        # Status transitioned to ready and counts are populated.
        assert result.status == "ready"
        assert result.num_pages == 2
        assert result.num_entities >= 2
        assert result.num_states >= 1
        assert result.num_scenes == 2
        assert result.num_beats >= 2
        assert result.num_shots >= 2
        assert result.num_spans >= 1
        assert result.principals  # fox + owl each appear in two beats

        async with factory() as s:
            reloaded = await BookRepo(s).get(book_id)
            assert reloaded is not None
            assert reloaded.status == BookStatus.READY
            assert reloaded.num_pages == 2

        # Progress fired at every milestone, importing → ready, ending at 1.0.
        stages = [stage for stage, _ in events]
        assert stages[0] == "importing"
        assert stages[-1] == "ready"
        for milestone in ("extract", "analyze", "canon", "shot_plan", "identity_lock"):
            assert milestone in stages
        assert events[-1][1] == 1.0

        # Page images + at least one locked keyframe were uploaded to storage.
        assert store.exists(page_image_key(book_id, 1))
        assert any(k.startswith("refs/") for k in store.data)

        # The source-span index resolves a focus word to a real shot.
        async with factory() as s:
            shot = await SourceSpanRepo(s).resolve_word_to_shot(book_id, 0)
            assert shot is not None
    finally:
        if book_id:
            async with factory() as s:
                await s.execute(delete(Book).where(Book.id == book_id))
        await engine.dispose()
