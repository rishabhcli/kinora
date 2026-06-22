"""LIVE Phase A smoke — one real public-domain page end-to-end (TEXT + IMAGE, NO video).

Skipped unless ``KINORA_LIVE_TESTS`` is set, so CI never runs it. It ingests a
single short Brothers-Grimm paragraph with the REAL providers: Qwen-VL page
analysis, the real Adapter (streaming chat) for beats/shots, and a real keyframe
image for the principal character — then prints the beats, the entity list, the
keyframe, and a source-span lookup. It NEVER calls the Generator or any Wan
render. Run it (small spend) with:

    export DASHSCOPE_API_KEY=$(grep '^DASHSCOPE_API_KEY=' .env | cut -d= -f2-)
    KINORA_TEST_DATABASE_URL=postgresql+asyncpg://kinora:kinora@localhost:5546/kinora \
    KINORA_TEST_S3_ENDPOINT_URL=http://localhost:9100 \
    KINORA_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_ingest_live_smoke.py -s -rA
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select

from app.core.config import Settings
from app.db.models.beat import Beat
from app.db.models.book import Book
from app.db.repositories.book import BookRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.shot import SourceSpanRepo
from app.ingest.service import IngestOptions, ingest_pdf
from app.providers import ResilienceConfig, create_providers
from app.storage.object_store import ObjectStore
from tests.test_ingest_support import (
    build_test_pdf,
    committing_session_factory,
    create_schema,
    new_engine,
)

pytestmark = [
    pytest.mark.skipif(
        not os.getenv("KINORA_LIVE_TESTS"),
        reason="live DashScope smoke; set KINORA_LIVE_TESTS=1 to run",
    ),
    pytest.mark.skipif(
        not os.getenv("KINORA_TEST_DATABASE_URL"), reason="KINORA_TEST_DATABASE_URL not set"
    ),
    pytest.mark.skipif(
        not os.getenv("KINORA_TEST_S3_ENDPOINT_URL"), reason="KINORA_TEST_S3_ENDPOINT_URL not set"
    ),
]

# "The Frog King" (Brothers Grimm) — public domain. One short page.
_GRIMM = (
    "In olden times when wishing still helped one, there lived a king whose "
    "daughters were all beautiful, but the youngest was so beautiful that the "
    "sun itself, which has seen so much, was astonished whenever it shone on her "
    "face. Close by the king's castle lay a great dark forest, and under an old "
    "lime-tree in the forest was a well. When the day was very warm, the king's "
    "youngest daughter went out into the forest and sat down by the side of the "
    "cool fountain, and she took a golden ball and threw it up on high and caught "
    "it, and this ball was her favourite plaything."
)


def _live_store() -> ObjectStore:
    store = ObjectStore(
        endpoint_url=os.environ["KINORA_TEST_S3_ENDPOINT_URL"],
        region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
        access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
        secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
        bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora-test"),
    )
    store.ensure_bucket()
    return store


async def test_live_ingest_one_grimm_page() -> None:
    settings = Settings()  # reads the REAL DASHSCOPE_API_KEY from the environment
    providers = create_providers(
        settings, resilience=ResilienceConfig(default_timeout_s=180.0, max_attempts=2)
    )
    store = _live_store()
    engine = new_engine()
    await create_schema(engine)
    factory = committing_session_factory(engine)

    book_id = ""
    try:
        async with factory() as session:
            book = await BookRepo(session).create(
                title="The Frog King (smoke)", art_direction="painterly storybook"
            )
            book_id = book.id

        result = await ingest_pdf(
            book_id,
            build_test_pdf([_GRIMM]),
            providers=providers,
            blob_store=store,
            settings=settings,
            session_factory=factory,
            options=IngestOptions(min_beats=1, poses=("front",), analyze_concurrency=1),
        )

        async with factory() as session:
            stmt = select(Beat).where(Beat.book_id == book_id).order_by(Beat.beat_index)
            beats = (await session.execute(stmt)).scalars().all()
            entities = await EntityRepo(session).list_active_at_beat(book_id, 1)
            spans = SourceSpanRepo(session)
            sample = await spans.resolve_word_to_shot(book_id, 0)
            mid_shot = await spans.resolve_word_to_shot(book_id, 30)

        print(
            f"\n[LIVE INGEST] status={result.status} pages={result.num_pages} "
            f"words={result.total_words} entities={result.num_entities} "
            f"scenes={result.num_scenes} beats={result.num_beats} "
            f"shots={result.num_shots} spans={result.num_spans} "
            f"principals={result.principals}"
        )

        print("\n[BEATS]")
        for beat in beats:
            print(f"  {beat.id} (idx {beat.beat_index}) {beat.summary!r}")
            print(f"     entities={beat.entities} mood={beat.mood!r} span={beat.source_span}")

        print("\n[ENTITIES]")
        for ent in entities:
            refs = (ent.appearance or {}).get("reference_images")
            voice = (ent.voice or {}).get("voice_id")
            print(
                f"  {ent.entity_key} [{ent.type.value}] {ent.name!r} v{ent.version} "
                f"voice={voice} desc={(ent.description or '')[:70]!r}"
            )
            if refs:
                print(f"     locked_refs={[r.get('key') for r in refs]}")

        print(
            f"\n[SPAN LOOKUP] word 0 -> {sample.id if sample else None}; "
            f"word 30 -> {mid_shot.id if mid_shot else None}"
        )

        # Confirm at least one principal keyframe image was actually produced.
        keyframe_keys = _refs_in_store(store, list(entities))
        print(f"[KEYFRAMES] {keyframe_keys}")

        # Assertions: real beats, entities, a resolvable span, and a keyframe.
        assert result.status == "ready"
        assert beats, "no beats produced"
        assert entities, "no entities produced"
        assert sample is not None, "source-span lookup returned no shot"
        assert result.principals, "no principal characters were locked"
        assert keyframe_keys, "no keyframe image was uploaded"
        for key in keyframe_keys:
            assert store.exists(key)
            assert store.get_bytes(key)[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        if book_id:
            async with factory() as session:
                await session.execute(delete(Book).where(Book.id == book_id))
        await providers.aclose()
        await engine.dispose()


def _refs_in_store(store: ObjectStore, entities: list) -> list[str]:
    """Collect the locked reference-image keys recorded on the canon entities."""
    keys: list[str] = []
    for ent in entities:
        for ref in (ent.appearance or {}).get("reference_images") or []:
            key = ref.get("key")
            if isinstance(key, str):
                keys.append(key)
    return keys
