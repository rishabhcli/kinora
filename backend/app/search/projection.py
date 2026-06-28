"""Project canon/library ORM objects into :class:`SearchDocument`s.

Pure functions, no DB / no network: each takes one ORM row and returns the
flattened, index-ready document. The indexing pipeline (:mod:`app.search.pipeline`)
reads the rows and the embeddings; this module owns only the *shape* of each
document — which fields become title / body / keywords / facets / numbers — so
the mapping is unit-testable in isolation.

The text fields are chosen so a free-text search surfaces the right object:
* a **book** is found by title/author;
* a **page** by its extracted text;
* a **scene** by its title;
* a **beat** by its summary + described visuals + mood;
* a **canon entity** by its name/aliases/description/appearance (the §8.1 node);
* a **shot** by its prompt + narration text.
"""

from __future__ import annotations

from app.db.models.beat import Beat
from app.db.models.book import Book, Page
from app.db.models.entity import Entity
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.search.documents import DocKind, SearchDocument, make_doc_id


def project_book(book: Book) -> SearchDocument:
    """A book → a BOOK document (title + author + art direction)."""
    body_parts = [p for p in (book.author, book.art_direction) if p]
    return SearchDocument(
        doc_id=make_doc_id(DocKind.BOOK, book.id),
        kind=DocKind.BOOK,
        ref_id=book.id,
        book_id=book.id,
        title=book.title or "",
        body=" ".join(body_parts),
        keywords=[k for k in (book.author,) if k],
        facets={"status": book.status.value if book.status else ""},
        numbers={"num_pages": float(book.num_pages)} if book.num_pages else {},
        payload={"author": book.author, "num_pages": book.num_pages, "boost": 2.0},
    )


def project_page(page: Page) -> SearchDocument:
    """A page → a PAGE document (its extracted text)."""
    return SearchDocument(
        doc_id=make_doc_id(DocKind.PAGE, page.id),
        kind=DocKind.PAGE,
        ref_id=page.id,
        book_id=page.book_id,
        title=f"Page {page.page_number}",
        body=page.text or "",
        facets={},
        numbers={"page_number": float(page.page_number)},
        payload={"page_number": page.page_number},
    )


def project_scene(scene: Scene) -> SearchDocument:
    """A scene → a SCENE document (its title + page span)."""
    return SearchDocument(
        doc_id=make_doc_id(DocKind.SCENE, scene.id),
        kind=DocKind.SCENE,
        ref_id=scene.id,
        book_id=scene.book_id,
        title=scene.title or f"Scene {scene.scene_index}",
        body="",
        keywords=[scene.style_entity_key] if scene.style_entity_key else [],
        facets={"style": scene.style_entity_key or ""},
        numbers={
            "scene_index": float(scene.scene_index),
            "page_start": float(scene.page_start),
            "page_end": float(scene.page_end),
        },
        payload={
            "scene_index": scene.scene_index,
            "page_start": scene.page_start,
            "page_end": scene.page_end,
        },
    )


def project_beat(beat: Beat) -> SearchDocument:
    """A beat → a BEAT document (summary + described visuals + mood + entities)."""
    body_parts = [p for p in (beat.summary, beat.described_visuals, beat.mood) if p]
    return SearchDocument(
        doc_id=make_doc_id(DocKind.BEAT, beat.id),
        kind=DocKind.BEAT,
        ref_id=beat.id,
        book_id=beat.book_id,
        title=(beat.summary or "")[:120],
        body=" ".join(body_parts),
        keywords=list(beat.entities or []),
        facets={"scene_id": beat.scene_id},
        numbers={"beat_index": float(beat.beat_index)},
        payload={
            "scene_id": beat.scene_id,
            "beat_index": beat.beat_index,
            "entities": list(beat.entities or []),
        },
    )


def project_entity(entity: Entity) -> SearchDocument | None:
    """A canon entity version → an ENTITY document (the §8.1 node, present only).

    Only the *currently active* version (``valid_to_beat is None``) is projected
    so search surfaces the live canon, not superseded versions — consistent with
    the §8.5 forgetting policy (retired facts drop out of forward retrieval). The
    pipeline pre-filters to active versions, but this also guards directly.
    """
    if entity.valid_to_beat is not None:
        return None
    appearance = entity.appearance or {}
    body_parts = [
        p
        for p in (
            entity.description,
            appearance.get("description") if isinstance(appearance, dict) else None,
        )
        if p
    ]
    keywords = list(entity.aliases or [])
    return SearchDocument(
        doc_id=make_doc_id(DocKind.ENTITY, f"{entity.book_id}:{entity.entity_key}"),
        kind=DocKind.ENTITY,
        ref_id=entity.entity_key,
        book_id=entity.book_id,
        title=entity.name or entity.entity_key,
        body=" ".join(body_parts),
        keywords=keywords,
        facets={"entity_type": entity.type.value if entity.type else ""},
        numbers={"version": float(entity.version)},
        embedding=list(entity.embedding) if entity.embedding is not None else None,
        payload={
            "entity_key": entity.entity_key,
            "entity_type": entity.type.value if entity.type else None,
            "version": entity.version,
            "aliases": keywords,
        },
    )


def project_shot(shot: Shot) -> SearchDocument:
    """A shot → a SHOT document (prompt + narration text + QA, the §8.2 record)."""
    narration = shot.narration or {}
    narr_text = narration.get("text") if isinstance(narration, dict) else None
    body_parts = [p for p in (shot.prompt, narr_text) if p]
    qa = shot.qa or {}
    facets: dict[str, str] = {"status": shot.status.value if shot.status else ""}
    if shot.render_mode:
        facets["render_mode"] = shot.render_mode
    if shot.scene_id:
        facets["scene_id"] = shot.scene_id
    numbers: dict[str, float] = {}
    if shot.duration_s is not None:
        numbers["duration_s"] = float(shot.duration_s)
    if isinstance(qa, dict) and isinstance(qa.get("score"), int | float):
        numbers["score"] = float(qa["score"])
    return SearchDocument(
        doc_id=make_doc_id(DocKind.SHOT, shot.id),
        kind=DocKind.SHOT,
        ref_id=shot.id,
        book_id=shot.book_id,
        title=(shot.prompt or shot.id)[:120],
        body=" ".join(body_parts),
        keywords=list(shot.reference_image_ids or []),
        facets=facets,
        numbers=numbers,
        embedding=list(shot.embedding) if shot.embedding is not None else None,
        payload={
            "scene_id": shot.scene_id,
            "beat_id": shot.beat_id,
            "render_mode": shot.render_mode,
            "verdict": qa.get("verdict") if isinstance(qa, dict) else None,
        },
    )


__all__ = [
    "project_beat",
    "project_book",
    "project_entity",
    "project_page",
    "project_scene",
    "project_shot",
]
