"""Canon build: cross-page entity dedup, the Style node, and initial states (§9.1 step 3)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.scene import SceneRepo
from app.ingest.analyze import AnalyzedEntity, AnalyzedState, PageAnalysis
from app.ingest.canon_build import STYLE_ENTITY_KEY, build_canon, entity_key_for, normalize_name
from app.memory.canon_service import CanonService
from tests.test_ingest_support import (
    FakeEmbedder,
    requires_db,
    session,  # noqa: F401  (pytest fixture)
)

pytestmark = requires_db


def _analyses() -> list[PageAnalysis]:
    """Two pages where the SAME character is named differently ("the fox" / "Fox")."""
    return [
        PageAnalysis(
            page_number=1,
            entities=[
                AnalyzedEntity(
                    name="the fox",
                    kind="character",
                    appearance="a small red fox",
                    aliases=["Reynard"],
                ),
                AnalyzedEntity(name="field", kind="location", appearance="a wide green field"),
            ],
            states=[AnalyzedState(subject="the fox", predicate="located_in", object="field")],
        ),
        PageAnalysis(
            page_number=2,
            entities=[
                # Same character, different surface form + a richer description.
                AnalyzedEntity(
                    name="Fox",
                    kind="character",
                    appearance="a small red fox with a long bushy tail and bright eyes",
                ),
                AnalyzedEntity(name="the owl", kind="character", appearance="a wise old grey owl"),
            ],
        ),
    ]


async def test_dedup_style_node_and_initial_state(session: AsyncSession) -> None:  # noqa: F811
    canon = CanonService(session, embedder=FakeEmbedder())
    book = await BookRepo(session).create(title="The Fox", art_direction="anime watercolor")

    result = await build_canon(
        canon, book_id=book.id, analyses=_analyses(), art_direction="anime watercolor"
    )

    # "the fox" and "Fox" deduplicate to ONE entity_key.
    assert (
        entity_key_for("character", "the fox")
        == entity_key_for("character", "Fox")
        == "char_fox"
    )
    keys = {e.entity_key for e in result.entities}
    assert keys == {"char_fox", "loc_field", "char_owl"}

    # That character is a single version-1 node (created once despite two pages).
    fox = await canon.get_entity(book.id, "char_fox")
    assert fox is not None and fox.version == 1
    # The merge keeps the most detailed appearance and captures the distinct alias.
    assert "bushy tail" in (fox.description or "")
    fox_entity = next(e for e in result.entities if e.entity_key == "char_fox")
    assert "Reynard" in fox_entity.aliases
    # The alias resolves to the same canon key (a beat naming "Reynard" finds the fox).
    assert result.alias_index[normalize_name("Reynard")] == "char_fox"

    # A Style node exists carrying the book's art-direction tokens.
    style = await canon.get_entity(book.id, STYLE_ENTITY_KEY)
    assert style is not None
    assert style.style_tokens is not None
    assert style.style_tokens["art_direction"] == "anime watercolor"
    assert "palette" in style.style_tokens and "lens" in style.style_tokens

    # An initial continuity state was asserted from the text (fox located_in field).
    assert result.num_states == 1
    states = await canon.active_states_at_beat(book.id, 1)
    located = [s for s in states if s.predicate == "located_in"]
    assert located and located[0].subject_entity_key == "char_fox"
    # The object resolved to the canon location key (not the raw word).
    assert located[0].object_value == "loc_field"


async def test_genesis_canon_visible_at_the_real_first_beat(
    session: AsyncSession,  # noqa: F811
) -> None:
    """Regression: the real first beat of every book gets ``beat_index=0`` (a
    0-based ordinal — see ``app/ingest/shot_plan.py``'s ``beat_index = 0`` seed
    and ``app/agents/adapter.py``'s ``index = beat_index_start + offset``), so
    genesis canon must be valid *as of* beat 0.

    ``CanonService.query()`` sets ``ordinal = beat.beat_index`` and resolves
    canon with ``Entity.valid_from_beat <= ordinal`` (``app/db/repositories/
    entity.py``). If genesis entities/states are stamped ``valid_from_beat=1``
    instead of ``0``, that filter silently excludes every character, location,
    and style token from the opening beat of every book.
    """
    canon = CanonService(session, embedder=FakeEmbedder())
    book = await BookRepo(session).create(title="The Fox", art_direction="anime watercolor")

    await build_canon(
        canon, book_id=book.id, analyses=_analyses(), art_direction="anime watercolor"
    )

    scene = await SceneRepo(session).create(
        book_id=book.id,
        scene_index=1,
        page_start=1,
        page_end=2,
        style_entity_key=STYLE_ENTITY_KEY,
    )
    # The Adapter's beat_index is 0-based; the real first beat of the book is 0.
    beat = await BeatRepo(session).create(
        book_id=book.id,
        scene_id=scene.id,
        beat_index=0,
        summary="the fox stands in the field",
        entities=["char_fox", "loc_field"],
    )

    canon_slice = await canon.query(book.id, beat.id)

    assert [c.entity_key for c in canon_slice.characters] == ["char_fox"]
    assert canon_slice.location is not None
    assert canon_slice.location.entity_key == "loc_field"
    assert canon_slice.style is not None
    assert canon_slice.style.entity_key == STYLE_ENTITY_KEY
    assert canon_slice.active_states, "the initial 'fox located_in field' state must be active"


async def test_known_names_cover_aliases(session: AsyncSession) -> None:  # noqa: F811
    canon = CanonService(session, embedder=FakeEmbedder())
    book = await BookRepo(session).create(title="Aliases")
    analyses = [
        PageAnalysis(
            page_number=1,
            entities=[
                AnalyzedEntity(
                    name="the Snow Queen",
                    kind="character",
                    appearance="a regal woman in ice-blue",
                    aliases=["Elsa"],
                )
            ],
        )
    ]
    result = await build_canon(canon, book_id=book.id, analyses=analyses)

    # The alias index resolves both the name and the alias to the same key.
    key = result.alias_index[normalize_name("the Snow Queen")]
    assert result.alias_index[normalize_name("Elsa")] == key
    assert "Elsa" in result.known_names
