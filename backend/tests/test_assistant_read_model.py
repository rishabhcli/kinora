"""Infra-gated tests for the DB-backed canon read model (pages/entities/shots/beats).

These exercise the real :class:`DbCanonReadModel` against the throwaway Postgres:
candidate projection across sources, spoiler-ordinal stamping, and position →
beat ceiling resolution. They skip cleanly with no infra (the unit suite covers
the logic via the fake read model).
"""

from __future__ import annotations

from httpx import AsyncClient

from app.assistant.read_model import DbCanonReadModel
from app.assistant.types import ReadingPosition, SourceKind
from app.composition import Container
from app.db.models.enums import EntityType, ShotStatus
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import PageRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo
from tests.conftest import requires_infra, seed_owned_book

pytestmark = requires_infra

Headers = dict[str, str]


async def _seed_book(api_client: AsyncClient, container: Container, headers: Headers) -> str:
    book_id = await seed_owned_book(api_client, container, headers)
    async with container.session_factory() as session:
        await SceneRepo(session).create(
            book_id=book_id, scene_index=0, page_start=1, page_end=2, scene_id="scene_a"
        )
        beats = BeatRepo(session)
        await beats.create(
            book_id=book_id,
            scene_id="scene_a",
            beat_index=1,
            summary="Elsa stands at the window.",
            beat_id="beat_1",
            source_span={"page": 1, "word_range": [0, 20]},
        )
        await beats.create(
            book_id=book_id,
            scene_id="scene_a",
            beat_index=5,
            summary="Elsa flees to the mountain and builds an ice castle.",
            beat_id="beat_5",
            source_span={"page": 2, "word_range": [200, 240]},
        )
        await PageRepo(session).create(
            book_id=book_id, page_number=1, text="Elsa stood alone at the frosted window."
        )
        await PageRepo(session).create(
            book_id=book_id, page_number=2, text="She climbed the north mountain in the storm."
        )
        ents = EntityRepo(session)
        await ents.upsert_new_version(
            book_id=book_id,
            entity_key="char_elsa",
            entity_type=EntityType.CHARACTER,
            name="Elsa",
            valid_from_beat=1,
            description="A young woman with a platinum braid.",
        )
        # A FUTURE character introduced at beat 9 — must be ordinal-stamped 9.
        await ents.upsert_new_version(
            book_id=book_id,
            entity_key="char_duke",
            entity_type=EntityType.CHARACTER,
            name="Duke",
            valid_from_beat=9,
            description="The scheming Duke of Weselton.",
        )
        await ShotRepo(session).create(
            id="shot_1",
            book_id=book_id,
            scene_id="scene_a",
            beat_id="beat_5",
            status=ShotStatus.ACCEPTED,
            duration_s=5.0,
            narration={"text": "A wide shot of the ice castle rising from the peak."},
        )
    return book_id


async def test_candidate_spans_cover_all_sources(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    book_id = await _seed_book(api_client, container, auth_headers)
    async with container.session_factory() as session:
        rm = DbCanonReadModel(session)
        spans = await rm.candidate_spans(book_id)
    kinds = {s.kind for s in spans}
    assert SourceKind.PAGE in kinds
    assert SourceKind.CANON in kinds
    assert SourceKind.BEAT in kinds
    assert SourceKind.SHOT in kinds


async def test_canon_spans_stamped_with_valid_from_beat(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    book_id = await _seed_book(api_client, container, auth_headers)
    async with container.session_factory() as session:
        rm = DbCanonReadModel(session)
        spans = await rm.candidate_spans(book_id, kinds=[SourceKind.CANON])
    by_key = {(s.meta or {}).get("entity_key"): s for s in spans}
    assert by_key["char_elsa"].ordinal == 1
    assert by_key["char_duke"].ordinal == 9  # future character


async def test_resolve_ceiling_from_word_position(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    book_id = await _seed_book(api_client, container, auth_headers)
    async with container.session_factory() as session:
        rm = DbCanonReadModel(session)
        # word 50 is past beat_1 (word 0) but before beat_5 (word 200) → ceiling 1.
        ceiling = await rm.resolve_ceiling_beat(
            ReadingPosition(book_id=book_id, word_index=50)
        )
    assert ceiling == 1


async def test_resolve_ceiling_explicit_beat_wins(
    api_client: AsyncClient, container: Container, auth_headers: Headers
) -> None:
    book_id = await _seed_book(api_client, container, auth_headers)
    async with container.session_factory() as session:
        rm = DbCanonReadModel(session)
        ceiling = await rm.resolve_ceiling_beat(
            ReadingPosition(book_id=book_id, beat_index=3, word_index=999)
        )
    assert ceiling == 3
