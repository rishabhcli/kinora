"""Real Postgres+pgvector integration tests for the data layer.

These run against a throwaway pgvector Postgres and SKIP cleanly when
``KINORA_TEST_DATABASE_URL`` is unset. The session fixture lives here (rather
than in the shared ``conftest.py``, which is out of scope for this phase) and
isolates each test by rolling back the transaction on teardown — repositories
only ``flush``, never ``commit``, so a rollback fully cleans up.

Covers the design-critical behaviours:
* (a) entity versioning + as-of-beat time-travel reads (§8.1),
* (b) continuity forgetting via interval scoping (§8.5),
* (c) the O(log n) source-span → shot resolution (§4.2),
plus pgvector episodic search (§8.2) and a smoke test of every other repo.
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
from app.db.base import Base
from app.db.models.enums import (
    BookStatus,
    EntityType,
    RenderJobStatus,
    RenderPriority,
    ShotStatus,
)
from app.db.repositories.book import BookRepo, PageRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.defect import DefectRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.pref import PrefsRepo
from app.db.repositories.render_job import RenderJobRepo
from app.db.repositories.session import SessionRepo
from app.db.repositories.shot import ShotCacheRepo, ShotRepo, SourceSpanRepo
from app.db.repositories.user import UserRepo

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

# A 768-d embedding pointing in one direction, padded out to the column width.
_DIM = 768


def _vec(*head: float) -> list[float]:
    """Build a 768-d vector from a few leading components (rest zero)."""
    return list(head) + [0.0] * (_DIM - len(head))


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an isolated session; rolls back all writes on teardown."""
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


# --- (a) entity versioning + as-of-beat -------------------------------------


async def test_entity_versioning_as_of_beat(session: AsyncSession) -> None:
    books = BookRepo(session)
    entities = EntityRepo(session)

    book = await books.create(title="The Snow Queen", author="Andersen")

    v1 = await entities.upsert_new_version(
        book_id=book.id,
        entity_key="char_elsa",
        entity_type=EntityType.CHARACTER,
        name="Elsa",
        valid_from_beat=1,
        appearance={"description": "platinum braid, ice-blue gown", "locked": False},
    )
    v2 = await entities.upsert_new_version(
        book_id=book.id,
        entity_key="char_elsa",
        entity_type=EntityType.CHARACTER,
        name="Elsa, the Snow Queen",
        valid_from_beat=10,
        appearance={"description": "ice crown, frost cloak", "locked": True},
    )
    assert (v1, v2) == (1, 2)

    early = await entities.get_as_of_beat(book.id, "char_elsa", 5)
    late = await entities.get_as_of_beat(book.id, "char_elsa", 15)
    boundary = await entities.get_as_of_beat(book.id, "char_elsa", 10)

    assert early is not None and early.version == 1 and early.name == "Elsa"
    assert late is not None and late.version == 2 and late.name == "Elsa, the Snow Queen"
    # The boundary beat resolves to the newer version.
    assert boundary is not None and boundary.version == 2
    # v1's interval was closed when v2 was asserted, and it supersedes v1.
    assert early.valid_to_beat == 10
    assert late.supersedes == early.id

    # list_active_at_beat returns exactly one (latest) row per entity_key.
    active = await entities.list_active_at_beat(book.id, 15)
    assert [(e.entity_key, e.version) for e in active] == [("char_elsa", 2)]
    # kind filter
    assert await entities.list_active_at_beat(book.id, 15, kinds=[EntityType.LOCATION]) == []

    # set_embedding round-trips through the pgvector column.
    await entities.set_embedding(late.id, _vec(0.1, 0.2, 0.3))
    await session.refresh(late)
    assert late.embedding is not None
    assert len(late.embedding) == _DIM


# --- (b) continuity forgetting ----------------------------------------------


async def test_continuity_forgetting_via_interval(session: AsyncSession) -> None:
    books = BookRepo(session)
    states = ContinuityStateRepo(session)

    book = await books.create(title="Hero's Tale")
    state_id = await states.assert_state(
        book_id=book.id,
        subject_entity_key="char_hero",
        predicate="possesses",
        object_value="prop_sword",
        valid_from_beat=12,
        source_span={"page": 8, "word_range": [1203, 1280]},
    )

    # Active before retirement.
    assert any(s.id == state_id for s in await states.active_states_at_beat(book.id, 15))

    # Forgetting: close the interval at beat 34.
    await states.retire_state(state_id, valid_to_beat=34)

    # Still valid inside the interval (and at the closing boundary)...
    assert any(s.id == state_id for s in await states.active_states_at_beat(book.id, 20))
    assert any(s.id == state_id for s in await states.active_states_at_beat(book.id, 34))
    # ...but invisible to forward generation after it.
    assert all(s.id != state_id for s in await states.active_states_at_beat(book.id, 50))

    # Subject scoping still works and excludes the retired fact at beat 50.
    scoped = await states.active_states_at_beat(book.id, 50, subject_entity_key="char_hero")
    assert scoped == []


# --- (c) source-span resolution ---------------------------------------------


async def test_source_span_resolution(session: AsyncSession) -> None:
    books = BookRepo(session)
    shots = ShotRepo(session)
    spans = SourceSpanRepo(session)

    book = await books.create(title="Spans")
    shot_a = await shots.create(book_id=book.id, scene_id="scene_001", beat_id="beat_0001")
    shot_b = await shots.create(book_id=book.id, scene_id="scene_001", beat_id="beat_0002")
    shot_c = await shots.create(book_id=book.id, scene_id="scene_002", beat_id="beat_0003")

    inserted = await spans.bulk_insert(
        [
            {
                "book_id": book.id,
                "word_index_start": 10,
                "word_index_end": 109,
                "shot_id": shot_a.id,
                "scene_id": "scene_001",
                "beat_id": "beat_0001",
            },
            {
                "book_id": book.id,
                "word_index_start": 110,
                "word_index_end": 209,
                "shot_id": shot_b.id,
                "scene_id": "scene_001",
                "beat_id": "beat_0002",
            },
            {
                "book_id": book.id,
                "word_index_start": 210,
                "word_index_end": 309,
                "shot_id": shot_c.id,
                "scene_id": "scene_002",
                "beat_id": "beat_0003",
            },
        ]
    )
    assert inserted == 3

    # A scroll position inside each span resolves to the right shot.
    resolved_a = await spans.resolve_word_to_shot(book.id, 50)
    resolved_b = await spans.resolve_word_to_shot(book.id, 150)
    resolved_c = await spans.resolve_word_to_shot(book.id, 250)
    assert resolved_a is not None and resolved_a.id == shot_a.id
    assert resolved_b is not None and resolved_b.id == shot_b.id
    assert resolved_c is not None and resolved_c.id == shot_c.id
    # Before the first span there is no shot yet.
    assert await spans.resolve_word_to_shot(book.id, 5) is None

    # The next not-yet-accepted shot after a word is the buffer's next target.
    nxt = await spans.next_uncommitted_shot(book.id, 50)
    assert nxt is not None and nxt.id == shot_b.id
    # Once B is accepted (committed), the next uncommitted shot is C.
    await shots.mark_accepted(shot_b.id)
    nxt2 = await spans.next_uncommitted_shot(book.id, 50)
    assert nxt2 is not None and nxt2.id == shot_c.id


# --- pgvector episodic search -----------------------------------------------


async def test_episodic_search_cosine_nearest(session: AsyncSession) -> None:
    books = BookRepo(session)
    shots = ShotRepo(session)

    book = await books.create(title="Episodic")
    near = await shots.create(
        book_id=book.id, beat_id="b1", status=ShotStatus.ACCEPTED, embedding=_vec(1.0, 0.0, 0.0)
    )
    mid = await shots.create(
        book_id=book.id, beat_id="b2", status=ShotStatus.ACCEPTED, embedding=_vec(0.7, 0.7, 0.0)
    )
    await shots.create(
        book_id=book.id, beat_id="b3", status=ShotStatus.ACCEPTED, embedding=_vec(0.0, 0.0, 1.0)
    )
    # A planned (not accepted) shot must be excluded even if it is the nearest.
    planned = await shots.create(
        book_id=book.id, beat_id="b4", status=ShotStatus.PLANNED, embedding=_vec(1.0, 0.0, 0.0)
    )

    results = await shots.episodic_search(book.id, _vec(1.0, 0.0, 0.0), k=2)
    assert [r.id for r in results] == [near.id, mid.id]
    assert planned.id not in {r.id for r in results}

    # Filters scope the search (no accepted shots for a different scene).
    filtered = await shots.episodic_search(
        book.id, _vec(1.0, 0.0, 0.0), filters={"scene_id": "missing"}
    )
    assert filtered == []


# --- remaining repositories (real, exercised) -------------------------------


async def test_repositories_smoke(session: AsyncSession) -> None:
    users = UserRepo(session)
    books = BookRepo(session)
    pages = PageRepo(session)
    prefs = PrefsRepo(session)
    sessions = SessionRepo(session)
    jobs = RenderJobRepo(session)
    defects = DefectRepo(session)
    cache = ShotCacheRepo(session)
    shots = ShotRepo(session)

    user = await users.create(email="reader@example.com", hashed_password="hashed")
    fetched = await users.get_by_email("reader@example.com")
    assert fetched is not None and fetched.id == user.id

    book = await books.create(title="Aesop's Fables", status=BookStatus.IMPORTING)
    await books.set_status(book.id, BookStatus.READY)
    await session.refresh(book)
    assert book.status == BookStatus.READY

    page = await pages.create(
        book_id=book.id,
        page_number=1,
        text="Once upon a time",
        word_boxes=[{"word_index": 0, "text": "Once", "bbox": [0.1, 0.1, 0.05, 0.02]}],
    )
    by_number = await pages.get_by_number(book.id, 1)
    assert by_number is not None and by_number.id == page.id

    # A repeated edit nudges the same preference row and accumulates weight.
    pref = await prefs.upsert_nudge(kind="pacing", value={"dir": "slower"}, user_id=user.id)
    pref2 = await prefs.upsert_nudge(
        kind="pacing", value={"dir": "slower"}, user_id=user.id, weight_delta=0.5
    )
    assert pref.id == pref2.id and pref2.weight == 1.5

    await sessions.upsert(
        session_id="sess_test", book_id=book.id, user_id=user.id, focus_word=10, velocity_wps=3.5
    )
    await sessions.update_fields("sess_test", focus_word=42)
    reloaded = await sessions.get("sess_test")
    assert reloaded is not None and reloaded.focus_word == 42

    shot = await shots.create(book_id=book.id, beat_id="b1", status=ShotStatus.PLANNED)
    job = await jobs.create(
        priority=RenderPriority.COMMITTED,
        session_id="sess_test",
        shot_id=shot.id,
        shot_hash="hash_1",
        reserved_video_s=5.0,
    )
    await jobs.set_status(job.id, RenderJobStatus.SUBMITTED)
    inflight = await jobs.list_inflight(session_id="sess_test")
    assert any(j.id == job.id for j in inflight)

    # Cache upsert: second put with the same hash overwrites the clip key.
    rec = await cache.put(
        shot_hash="hash_1", book_id=book.id, clip_key="clips/x.mp4", video_seconds=5.0
    )
    assert rec.clip_key == "clips/x.mp4"
    await cache.put(
        shot_hash="hash_1", book_id=book.id, clip_key="clips/y.mp4", video_seconds=5.0
    )
    cached = await cache.get("hash_1")
    assert cached is not None and cached.clip_key == "clips/y.mp4"

    defect = await defects.log(
        book_id=book.id, kind="qa_fail", shot_id=shot.id, detail={"reason": "style_drift"}
    )
    assert defect.id is not None
    assert any(d.id == defect.id for d in await defects.list_for_book(book.id))
