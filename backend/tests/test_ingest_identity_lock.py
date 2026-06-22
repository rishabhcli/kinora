"""Identity lock: principal keyframes (locked refs + embedding) + distinct voices (§9.1 step 5)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.scene import SceneRepo
from app.ingest.analyze import AnalyzedEntity, PageAnalysis
from app.ingest.canon_build import CanonBuildResult, build_canon
from app.ingest.identity_lock import (
    NARRATOR_ENTITY_KEY,
    NARRATOR_VOICE,
    PRESET_VOICES,
    lock_identities,
)
from app.memory.canon_service import CanonService
from app.providers import Providers
from tests.test_ingest_support import (
    EMBED_DIM,
    FakeEmbedder,
    MemoryBlobStore,
    providers,  # noqa: F401  (pytest fixture)
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

pytestmark = requires_db

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-keyframe-bytes"


async def _seed_canon_and_beats(
    session: AsyncSession,  # noqa: F811
    store: MemoryBlobStore,
) -> tuple[str, CanonService, CanonBuildResult]:
    """Build a 3-character canon and beats so fox+owl are principals, mouse is not."""
    canon = CanonService(session, embedder=FakeEmbedder(), blob_store=store)
    book = await BookRepo(session).create(title="Principals")
    analyses = [
        PageAnalysis(
            page_number=1,
            entities=[
                AnalyzedEntity(name="Fox", kind="character", appearance="a small red fox"),
                AnalyzedEntity(name="Owl", kind="character", appearance="a wise old grey owl"),
                AnalyzedEntity(name="Mouse", kind="character", appearance="a tiny grey mouse"),
            ],
        )
    ]
    build_result = await build_canon(canon, book_id=book.id, analyses=analyses)
    await SceneRepo(session).create(
        book_id=book.id, scene_index=1, page_start=1, page_end=1, scene_id="scene_001"
    )
    # fox in beats 0,1; owl in beats 1,2; mouse only in beat 2.
    beats = BeatRepo(session)
    await beats.create(
        book_id=book.id, scene_id="scene_001", beat_index=0, summary="fox runs",
        entities=["char_fox"], beat_id="beat_0000",
    )
    await beats.create(
        book_id=book.id, scene_id="scene_001", beat_index=1, summary="fox meets owl",
        entities=["char_fox", "char_owl"], beat_id="beat_0001",
    )
    await beats.create(
        book_id=book.id, scene_id="scene_001", beat_index=2, summary="owl and mouse",
        entities=["char_owl", "char_mouse"], beat_id="beat_0002",
    )
    return book.id, canon, build_result


async def test_identity_lock_keyframes_and_distinct_voices(
    session: AsyncSession,  # noqa: F811
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryBlobStore()
    book_id, canon, build_result = await _seed_canon_and_beats(session, store)
    characters = build_result.characters()

    calls: list[str] = []

    async def fake_generate(prompt: str, **kwargs: object) -> list[bytes]:
        calls.append(prompt)
        return [_PNG]

    monkeypatch.setattr(providers.image, "generate", fake_generate)

    result = await lock_identities(
        book_id=book_id,
        canon=canon,
        characters=characters,
        providers=providers,
        blob_store=store,
        style_tokens={"art_direction": "storybook", "palette": "warm", "lens": "35mm"},
        min_beats=2,
    )

    # fox + owl appear in ≥2 beats → principals; mouse (1 beat) is not.
    assert set(result.principals) == {"char_fox", "char_owl"}
    assert "char_mouse" not in result.principals
    assert len(calls) == 2  # one keyframe generated per principal

    # Locked reference images persisted + uploaded, and a NEW (locked) version made.
    fox = await canon.get_entity(book_id, "char_fox")
    assert fox is not None and fox.version == 2
    assert fox.appearance is not None and fox.appearance.get("locked") is True
    refs = fox.appearance.get("reference_images")
    assert refs and refs[0]["locked"] is True
    assert store.exists(refs[0]["key"])

    # The appearance embedding was computed from the locked reference image.
    fox_row = await EntityRepo(session).get_as_of_beat(book_id, "char_fox", 1)
    assert fox_row is not None and fox_row.embedding is not None
    assert len(fox_row.embedding) == EMBED_DIM

    # Each principal got a DISTINCT preset voice, and none is the narrator voice.
    preset_ids = {v.voice_id for v in PRESET_VOICES}
    fox_voice = result.voices["char_fox"]
    owl_voice = result.voices["char_owl"]
    assert fox_voice in preset_ids and owl_voice in preset_ids
    assert fox_voice != owl_voice
    assert NARRATOR_VOICE.voice_id not in {fox_voice, owl_voice}

    # The narrator is a dedicated entity carrying the reserved narration voice.
    narrator = await canon.get_entity(book_id, NARRATOR_ENTITY_KEY)
    assert narrator is not None and narrator.voice is not None
    assert narrator.voice["voice_id"] == NARRATOR_VOICE.voice_id
    assert narrator.voice["role"] == "narrator"
    # The stored voice is a preset (never a clone of nonexistent audio).
    assert fox.voice is not None and fox.voice["preset"] is True
