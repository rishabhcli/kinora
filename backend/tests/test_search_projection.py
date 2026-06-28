"""Unit tests for projecting canon/library ORM rows → SearchDocuments.

No DB session needed: ORM objects are constructed in-memory and projected.
"""

from __future__ import annotations

from app.db.models.beat import Beat
from app.db.models.book import Book, Page
from app.db.models.entity import Entity
from app.db.models.enums import BookStatus, EntityType, ShotStatus
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.search.documents import DocKind
from app.search.projection import (
    project_beat,
    project_book,
    project_entity,
    project_page,
    project_scene,
    project_shot,
)


def test_project_book() -> None:
    book = Book(
        id="b1", title="The Snow Queen", author="Andersen",
        status=BookStatus.READY, num_pages=120,
    )
    doc = project_book(book)
    assert doc.doc_id == "book:b1"
    assert doc.kind is DocKind.BOOK
    assert doc.title == "The Snow Queen"
    assert "Andersen" in doc.body
    assert doc.facets["status"] == "ready"
    assert doc.numbers["num_pages"] == 120.0


def test_project_page() -> None:
    page = Page(id="p1", book_id="b1", page_number=3, text="Gerda walked north.")
    doc = project_page(page)
    assert doc.kind is DocKind.PAGE
    assert doc.body == "Gerda walked north."
    assert doc.numbers["page_number"] == 3.0


def test_project_scene() -> None:
    scene = Scene(
        id="sc1", book_id="b1", scene_index=2, title="The Palace", page_start=10, page_end=14,
        style_entity_key="style_ice",
    )
    doc = project_scene(scene)
    assert doc.kind is DocKind.SCENE
    assert doc.title == "The Palace"
    assert doc.numbers["scene_index"] == 2.0
    assert "style_ice" in doc.keywords


def test_project_beat() -> None:
    beat = Beat(
        id="bt1", book_id="b1", scene_id="sc1", beat_index=7,
        summary="Gerda enters the palace.",
        entities=["char_gerda", "loc_palace"], described_visuals="ice everywhere", mood="tense",
    )
    doc = project_beat(beat)
    assert doc.kind is DocKind.BEAT
    assert "ice everywhere" in doc.body
    assert "char_gerda" in doc.keywords
    assert doc.numbers["beat_index"] == 7.0


def test_project_active_entity() -> None:
    entity = Entity(
        id="e1", book_id="b1", entity_key="char_gerda", type=EntityType.CHARACTER,
        name="Gerda", aliases=["the girl"], description="A brave girl.",
        appearance={"description": "red boots"}, version=2,
        valid_from_beat=1, valid_to_beat=None,
    )
    doc = project_entity(entity)
    assert doc is not None
    assert doc.doc_id == "entity:b1:char_gerda"
    assert doc.title == "Gerda"
    assert "red boots" in doc.body
    assert "the girl" in doc.keywords
    assert doc.facets["entity_type"] == "character"


def test_project_retired_entity_is_skipped() -> None:
    # A superseded version (valid_to_beat set) drops out of forward search (§8.5).
    entity = Entity(
        id="e0", book_id="b1", entity_key="char_gerda", type=EntityType.CHARACTER,
        name="Gerda (old)", version=1, valid_from_beat=1, valid_to_beat=20,
    )
    assert project_entity(entity) is None


def test_project_shot() -> None:
    shot = Shot(
        id="s1", book_id="b1", scene_id="sc1", beat_id="bt1", status=ShotStatus.ACCEPTED,
        render_mode="reference_to_video", prompt="Wide shot of the palace",
        narration={"text": "Gerda arrives."}, duration_s=5.0,
        qa={"verdict": "pass", "score": 0.9}, reference_image_ids=["char_gerda@v2"],
    )
    doc = project_shot(shot)
    assert doc.kind is DocKind.SHOT
    assert "Wide shot" in doc.title
    assert "Gerda arrives." in doc.body
    assert doc.facets["render_mode"] == "reference_to_video"
    assert doc.numbers["duration_s"] == 5.0
    assert doc.numbers["score"] == 0.9
