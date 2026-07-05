"""Canon retrieval policy, forgetting, and time-travel — the Track-1 core (§8.4/§8.5).

Real Postgres+pgvector integration tests (SKIP when ``KINORA_TEST_DATABASE_URL``
is unset). The embedder is a deterministic one-hot test double so episodic
retrieval is exercised without a network call (a real-embedding test is gated by
``KINORA_LIVE_TESTS`` in ``test_mcp_tools.py``). Each test rolls back on
teardown — the services only flush.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.models.enums import EntityType
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo
from app.memory.canon_service import CanonService
from app.memory.episodic_service import EpisodicService

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

_DIM = 1152


class FakeEmbedder:
    """Deterministic 1152-d one-hot embedder (a test double for ``Embedder``).

    Identical inputs map to identical unit vectors (cosine 1.0); different inputs
    map to orthogonal axes (cosine 0.0), so nearest-neighbour ordering is exact
    and offline.
    """

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one_hot(t.encode("utf-8")) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._one_hot(b) for b in images]

    @staticmethod
    def _one_hot(data: bytes) -> list[float]:
        axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % _DIM
        vector = [0.0] * _DIM
        vector[axis] = 1.0
        return vector


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


async def test_canon_query_returns_only_the_relevant_slice(session: AsyncSession) -> None:
    books = BookRepo(session)
    scenes = SceneRepo(session)
    beats = BeatRepo(session)
    canon = CanonService(session, embedder=FakeEmbedder())
    episodic = EpisodicService(shots=ShotRepo(session), embedder=FakeEmbedder())

    book = await books.create(title="The Quest")
    await scenes.create(
        book_id=book.id,
        scene_index=1,
        page_start=1,
        page_end=2,
        style_entity_key="style_book",
        scene_id="scene_001",
    )

    # char_hero has two versions: v1 from beat 1, v2 from beat 10.
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_hero",
        entity_type=EntityType.CHARACTER,
        name="Hero",
        valid_from_beat=1,
        appearance={
            "description": "a young knight",
            "reference_image_keys": ["refs/hero/front.png"],
            "locked": True,
        },
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_hero",
        entity_type=EntityType.CHARACTER,
        name="Hero the Brave",
        valid_from_beat=10,
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_villain",
        entity_type=EntityType.CHARACTER,
        name="Villain",
        valid_from_beat=1,
    )
    # An entity that exists in the book but is NOT present in this beat.
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_ghost",
        entity_type=EntityType.CHARACTER,
        name="Ghost",
        valid_from_beat=1,
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="loc_castle",
        entity_type=EntityType.LOCATION,
        name="Castle",
        valid_from_beat=1,
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="style_book",
        entity_type=EntityType.STYLE,
        name="Storybook",
        valid_from_beat=1,
        style_tokens={"palette": "warm", "lens": "35mm"},
    )

    await beats.create(
        book_id=book.id,
        scene_id="scene_001",
        beat_index=1,
        summary="hero enters the hall",
        entities=["char_hero", "loc_castle"],
        described_visuals="hero stands in the castle hall",
        beat_id="beat_0001",
    )
    await beats.create(
        book_id=book.id,
        scene_id="scene_001",
        beat_index=2,
        summary="the confrontation",
        entities=["char_hero", "char_villain", "loc_castle"],
        described_visuals="hero confronts the villain",
        beat_id="beat_0002",
    )

    # An accepted shot for beat_0001 → the previous endpoint frame AND an
    # episodic neighbour (its embedding matches beat_0002's described visuals).
    await episodic.log(
        book_id=book.id,
        beat_id="beat_0001",
        scene_id="scene_001",
        render_mode="reference_to_video",
        seed=7,
        output={"clip_key": "clips/a.mp4", "last_frame_key": "lastframes/a.png"},
        qa={"verdict": "pass", "ccs": 0.92},
        duration_s=5.0,
        described_visuals_text="hero confronts the villain",
    )

    result = await canon.query(book.id, "beat_0002")

    character_keys = {c.entity_key for c in result.characters}
    assert character_keys == {"char_hero", "char_villain"}

    # Resolved as of beat 2 → v1 (v2 only becomes valid at beat 10).
    hero = next(c for c in result.characters if c.entity_key == "char_hero")
    assert hero.version == 1
    assert hero.name == "Hero"
    assert any(r.key == "refs/hero/front.png" for r in hero.reference_images)

    assert result.location is not None
    assert result.location.entity_key == "loc_castle"
    assert result.style is not None
    assert result.style.entity_key == "style_book"
    assert result.style.style_tokens == {"palette": "warm", "lens": "35mm"}

    # Previous accepted endpoint frame (for continuation).
    assert result.previous_endpoint is not None
    assert result.previous_endpoint.last_frame_key == "lastframes/a.png"

    # Top-k prior accepted shots for similar beats.
    assert any(s.beat_id == "beat_0001" for s in result.episodic)

    # The unrelated entity never appears in any bucket of the slice.
    everywhere = (
        character_keys
        | {p.entity_key for p in result.props}
        | ({result.location.entity_key} if result.location else set())
        | ({result.style.entity_key} if result.style else set())
    )
    assert "char_ghost" not in everywhere


async def test_get_entity_time_travel_boundaries(session: AsyncSession) -> None:
    # Pins the §8.3 time-travel read at its interval boundaries — the subtle part:
    # before introduction → None; at the version-change beat the *newer* version
    # wins the tie (get_as_of_beat prefers the highest version).
    books = BookRepo(session)
    canon = CanonService(session, embedder=FakeEmbedder())
    book = await books.create(title="Time Travel")

    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_x",
        entity_type=EntityType.CHARACTER,
        name="X v1",
        valid_from_beat=5,
    )

    # Before it is introduced → not present.
    assert await canon.get_entity(book.id, "char_x", at_beat=1) is None
    # At the introduction beat → v1.
    at5 = await canon.get_entity(book.id, "char_x", at_beat=5)
    assert at5 is not None and at5.version == 1 and at5.name == "X v1"

    # A second version valid from beat 10.
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_x",
        entity_type=EntityType.CHARACTER,
        name="X v2",
        valid_from_beat=10,
    )
    # Just before the change → still v1.
    at9 = await canon.get_entity(book.id, "char_x", at_beat=9)
    assert at9 is not None and at9.version == 1
    # At the boundary beat → the newer version wins the tie.
    at10 = await canon.get_entity(book.id, "char_x", at_beat=10)
    assert at10 is not None and at10.version == 2 and at10.name == "X v2"
    # Latest (no beat) → v2.
    latest = await canon.get_entity(book.id, "char_x")
    assert latest is not None and latest.version == 2

    # An entity key that was never created → None (not an error).
    assert await canon.get_entity(book.id, "char_missing", at_beat=10) is None


async def test_canon_query_kind_filter(session: AsyncSession) -> None:
    books = BookRepo(session)
    scenes = SceneRepo(session)
    beats = BeatRepo(session)
    canon = CanonService(session, embedder=FakeEmbedder())

    book = await books.create(title="Filter")
    await scenes.create(book_id=book.id, scene_index=1, page_start=1, page_end=1, scene_id="sc")
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_a",
        entity_type=EntityType.CHARACTER,
        name="A",
        valid_from_beat=1,
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="loc_a",
        entity_type=EntityType.LOCATION,
        name="Loc",
        valid_from_beat=1,
    )
    await beats.create(
        book_id=book.id,
        scene_id="sc",
        beat_index=1,
        summary="s",
        entities=["char_a", "loc_a"],
        beat_id="b1",
    )

    only_chars = await canon.query(book.id, "b1", ["character"])
    assert {c.entity_key for c in only_chars.characters} == {"char_a"}
    assert only_chars.location is None  # location kind filtered out


async def test_forgetting_scopes_a_fact_to_its_interval(session: AsyncSession) -> None:
    books = BookRepo(session)
    scenes = SceneRepo(session)
    beats = BeatRepo(session)
    canon = CanonService(session, embedder=FakeEmbedder())

    book = await books.create(title="The Lost Sword")
    await scenes.create(
        book_id=book.id, scene_index=1, page_start=1, page_end=5, scene_id="scene_s"
    )
    # char_hero gains a second version at beat 30 (for the time-travel assertion).
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_hero",
        entity_type=EntityType.CHARACTER,
        name="Hero",
        valid_from_beat=1,
    )
    await canon.upsert_entity(
        book_id=book.id,
        entity_key="char_hero",
        entity_type=EntityType.CHARACTER,
        name="Hero, aged",
        valid_from_beat=30,
    )
    for idx in (12, 20, 35):
        await beats.create(
            book_id=book.id,
            scene_id="scene_s",
            beat_index=idx,
            summary=f"beat {idx}",
            entities=["char_hero"],
            beat_id=f"beat_{idx:04d}",
        )

    state_id = await canon.assert_state(
        book_id=book.id,
        subject_entity_key="char_hero",
        predicate="possesses",
        object_value="prop_sword",
        valid_from_beat=12,
    )

    # Active before retirement (canon.query at beat 20).
    before = await canon.query(book.id, "beat_0020")
    assert any(s.state_id == state_id for s in before.active_states)

    # Forgetting: close the interval at beat 34.
    await canon.retire_state(state_id, valid_to_beat=34)

    # Excluded from forward generation at beat 35 (35 > 34) ...
    after = await canon.query(book.id, "beat_0035")
    assert all(s.state_id != state_id for s in after.active_states)

    # ... but still visible for a time-travel read inside the interval (beat 20).
    travel = await canon.query(book.id, "beat_0020")
    assert any(s.state_id == state_id for s in travel.active_states)

    # active_states_at_beat passthrough mirrors the policy.
    assert any(s.state_id == state_id for s in await canon.active_states_at_beat(book.id, 20))
    assert all(s.state_id != state_id for s in await canon.active_states_at_beat(book.id, 50))

    # Regression (independent review, 2026-07-05): the retirement boundary
    # itself (valid_to_beat=34) must be excluded, not included — half-open
    # [12, 34), matching BeatInterval.contains_beat and the bitemporal engine
    # exactly. The read used to be closed ([12, 34]), so a fact and its
    # superseding successor could both read as active at this exact beat.
    assert all(s.state_id != state_id for s in await canon.active_states_at_beat(book.id, 34))
    # One beat earlier is still inside the interval.
    assert any(s.state_id == state_id for s in await canon.active_states_at_beat(book.id, 33))

    # Entity history is preserved across versions (time-travel reads).
    early = await canon.get_entity(book.id, "char_hero", at_beat=5)
    late = await canon.get_entity(book.id, "char_hero", at_beat=40)
    latest = await canon.get_entity(book.id, "char_hero")
    assert early is not None and early.version == 1 and early.name == "Hero"
    assert late is not None and late.version == 2
    assert latest is not None and latest.version == 2
