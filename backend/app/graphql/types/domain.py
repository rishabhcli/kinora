"""The Kinora public domain object types (books, pages, shots, scenes, …).

These are the *output* types the public schema exposes. Source values are the
SQLAlchemy ORM rows (or small projection dataclasses) loaded by the resolvers in
``app/graphql/resolvers/``; field resolvers translate a DB column into the
public field, presign object-store keys into URLs, and dataloader-batch the
relationship traversals (``Shot.book``, ``Scene.shots``, …) to avoid N+1.

Every persistent entity implements the ``Node`` interface (a global ``id`` + a
``node(id:)`` lookup), and list relationships are exposed as Relay connections.
The types are cached singletons so the assembled schema shares one registry.
"""

from __future__ import annotations

from typing import Any

from app.graphql.scalars import (
    GraphQLBoolean,
    GraphQLDateTime,
    GraphQLFloat,
    GraphQLID,
    GraphQLInt,
    GraphQLJSON,
    GraphQLString,
)
from app.graphql.type_system import (
    Argument,
    Field,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
)
from app.graphql.types.connections import connection_type
from app.graphql.types.enums import (
    BOOK_STATUS_ENUM,
    ENTITY_TYPE_ENUM,
    SESSION_MODE_ENUM,
    SHOT_STATUS_ENUM,
)
from app.graphql.types.node import NODE_INTERFACE, global_id

# --------------------------------------------------------------------------- #
# Cursor pagination args reused across connection fields
# --------------------------------------------------------------------------- #


def _connection_args(extra: dict[str, Argument] | None = None) -> dict[str, Argument]:
    from app.graphql.scalars import GraphQLCursor

    args = {
        "first": Argument(GraphQLInt, description="Forward page size (max 100)."),
        "after": Argument(GraphQLCursor, description="Return edges after this cursor."),
        "last": Argument(GraphQLInt, description="Backward page size (max 100)."),
        "before": Argument(GraphQLCursor, description="Return edges before this cursor."),
    }
    if extra:
        args.update(extra)
    return args


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def page_type() -> GraphQLObject:
    return _cache("Page", _build_page)


def _build_page() -> GraphQLObject:
    return GraphQLObject(
        "Page",
        lambda: {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_node_id("Page", "id")),
            "bookId": Field(GraphQLNonNull(GraphQLID), resolver=_attr("book_id")),
            "pageNumber": Field(GraphQLNonNull(GraphQLInt), resolver=_attr("page_number")),
            "text": Field(GraphQLString, resolver=_attr("text")),
            "imageUrl": Field(
                GraphQLString,
                resolver=lambda s, a, c, i: c.presign(getattr(s, "image_key", None)),
                description="Presigned GET URL for the rasterised page image.",
            ),
            "wordBoxes": Field(
                GraphQLJSON,
                resolver=lambda s, a, c, i: list(getattr(s, "word_boxes", None) or []),
                description="Per-word bounding boxes for karaoke highlighting (§9.4).",
            ),
        },
        interfaces=[NODE_INTERFACE],
        description="One rasterised page: image, text, and per-word boxes (§9.4).",
    )


# --------------------------------------------------------------------------- #
# Shot
# --------------------------------------------------------------------------- #


def shot_type() -> GraphQLObject:
    return _cache("Shot", _build_shot)


def _build_shot() -> GraphQLObject:
    from app.graphql.resolvers.shot import resolve_shot_book, resolve_shot_clip_url

    return GraphQLObject(
        "Shot",
        lambda: {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_node_id("Shot", "id")),
            "bookId": Field(GraphQLNonNull(GraphQLID), resolver=_attr("book_id")),
            "sceneId": Field(GraphQLID, resolver=_attr("scene_id")),
            "beatId": Field(GraphQLID, resolver=_attr("beat_id")),
            "status": Field(
                GraphQLNonNull(SHOT_STATUS_ENUM), resolver=_enum_attr("status")
            ),
            "renderMode": Field(GraphQLString, resolver=_attr("render_mode")),
            "durationSeconds": Field(GraphQLFloat, resolver=_attr("duration_s")),
            "sourceSpan": Field(
                GraphQLJSON,
                resolver=_attr("source_span"),
                description="{page, para, word_range:[start,end]} reading position (§4.2).",
            ),
            "qa": Field(
                GraphQLJSON,
                resolver=_attr("qa"),
                description="QA scores (CCS, drift, verdict, …) for this shot (§9.5).",
            ),
            "referenceImageIds": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLString))),
                resolver=lambda s, a, c, i: list(getattr(s, "reference_image_ids", None) or []),
            ),
            "clipUrl": Field(
                GraphQLString,
                resolver=resolve_shot_clip_url,
                description="Presigned GET URL for the rendered clip, if any.",
            ),
            "canonVersionAtRender": Field(
                GraphQLInt, resolver=_attr("canon_version_at_render")
            ),
            "createdAt": Field(GraphQLDateTime, resolver=_iso("created_at")),
            "book": Field(
                book_type(),
                resolver=resolve_shot_book,
                cost=2,
                description="The book this shot belongs to (dataloader-batched).",
            ),
        },
        interfaces=[NODE_INTERFACE],
        description="One generated clip and its episodic record (§8.2).",
    )


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #


def scene_type() -> GraphQLObject:
    return _cache("Scene", _build_scene)


def _build_scene() -> GraphQLObject:
    from app.graphql.resolvers.scene import resolve_scene_shots

    return GraphQLObject(
        "Scene",
        lambda: {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_node_id("Scene", "id")),
            "bookId": Field(GraphQLNonNull(GraphQLID), resolver=_attr("book_id")),
            "sceneIndex": Field(GraphQLNonNull(GraphQLInt), resolver=_attr("scene_index")),
            "title": Field(GraphQLString, resolver=_attr("title")),
            "pageStart": Field(GraphQLNonNull(GraphQLInt), resolver=_attr("page_start")),
            "pageEnd": Field(GraphQLNonNull(GraphQLInt), resolver=_attr("page_end")),
            "styleEntityKey": Field(GraphQLString, resolver=_attr("style_entity_key")),
            "shots": Field(
                GraphQLNonNull(connection_type(shot_type())),
                args=_connection_args(),
                resolver=resolve_scene_shots,
                cost=2,
                list_cost_multiplier=True,
                description="The accepted shots that compose this scene's film (§9.6).",
            ),
        },
        interfaces=[NODE_INTERFACE],
        description="A narrative scene — the stitching boundary (§4.2/§9.6).",
    )


# --------------------------------------------------------------------------- #
# Canon — entities + continuity states
# --------------------------------------------------------------------------- #


def canon_entity_type() -> GraphQLObject:
    return _cache("CanonEntity", _build_canon_entity)


def _build_canon_entity() -> GraphQLObject:
    return GraphQLObject(
        "CanonEntity",
        lambda: {
            "id": Field(
                GraphQLNonNull(GraphQLID),
                resolver=lambda s, a, c, i: global_id("CanonEntity", s.get("id", "")),
            ),
            "entityKey": Field(GraphQLNonNull(GraphQLString), resolver=_key("id")),
            "type": Field(GraphQLNonNull(ENTITY_TYPE_ENUM), resolver=_key("type")),
            "name": Field(GraphQLNonNull(GraphQLString), resolver=_key("name")),
            "aliases": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLString))),
                resolver=lambda s, a, c, i: list(s.get("aliases") or []),
            ),
            "description": Field(GraphQLString, resolver=_key("description")),
            "appearance": Field(GraphQLJSON, resolver=_key("appearance")),
            "styleTokens": Field(GraphQLJSON, resolver=_key("style_tokens")),
            "voice": Field(GraphQLJSON, resolver=_key("voice")),
            "version": Field(GraphQLNonNull(GraphQLInt), resolver=_key("version")),
            "validFromBeat": Field(GraphQLInt, resolver=_key("valid_from_beat")),
            "validToBeat": Field(GraphQLInt, resolver=_key("valid_to_beat")),
        },
        interfaces=[NODE_INTERFACE],
        description="A canon entity (current version) — character/location/prop/style (§8.1).",
    )


def canon_state_type() -> GraphQLObject:
    return _cache("CanonState", _build_canon_state)


def _build_canon_state() -> GraphQLObject:
    return GraphQLObject(
        "CanonState",
        {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_key("id")),
            "subjectEntityKey": Field(
                GraphQLNonNull(GraphQLString), resolver=_key("subject_entity_key")
            ),
            "predicate": Field(GraphQLNonNull(GraphQLString), resolver=_key("predicate")),
            "objectValue": Field(GraphQLNonNull(GraphQLString), resolver=_key("object_value")),
            "validFromBeat": Field(GraphQLNonNull(GraphQLInt), resolver=_key("valid_from_beat")),
            "validToBeat": Field(GraphQLInt, resolver=_key("valid_to_beat")),
            "version": Field(GraphQLNonNull(GraphQLInt), resolver=_key("version")),
            "active": Field(GraphQLNonNull(GraphQLBoolean), resolver=_key("active")),
            "sourceSpan": Field(GraphQLJSON, resolver=_key("source_span")),
        },
        description="A versioned continuity fact (subject, predicate, object) over beats (§8.5).",
    )


def canon_type() -> GraphQLObject:
    return _cache("Canon", _build_canon)


def _build_canon() -> GraphQLObject:
    return GraphQLObject(
        "Canon",
        lambda: {
            "bookId": Field(GraphQLNonNull(GraphQLID), resolver=_key("book_id")),
            "entities": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(canon_entity_type()))),
                resolver=lambda s, a, c, i: s.get("entities", []),
                cost=2,
            ),
            "states": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(canon_state_type()))),
                resolver=lambda s, a, c, i: s.get("states", []),
                cost=2,
            ),
            "markdown": Field(
                GraphQLString,
                resolver=_key("markdown"),
                description="The human-inspectable canon-vault markdown export (§8.1).",
            ),
        },
        description="A book's canon graph: entities + continuity facts + vault export (§8).",
    )


# --------------------------------------------------------------------------- #
# Book — the central node
# --------------------------------------------------------------------------- #


def book_type() -> GraphQLObject:
    return _cache("Book", _build_book)


def _build_book() -> GraphQLObject:
    from app.graphql.resolvers.book import (
        resolve_book_canon,
        resolve_book_pages,
        resolve_book_scenes,
        resolve_book_shots,
    )

    return GraphQLObject(
        "Book",
        lambda: {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_node_id("Book", "id")),
            "legacyId": Field(
                GraphQLID,
                resolver=_attr("id"),
                deprecation_reason=(
                    "Use `id` (the canonical book id). Kept as an alias for v0 clients."
                ),
            ),
            "title": Field(GraphQLNonNull(GraphQLString), resolver=_attr("title")),
            "author": Field(GraphQLString, resolver=_attr("author")),
            "status": Field(GraphQLNonNull(BOOK_STATUS_ENUM), resolver=_enum_attr("status")),
            "numPages": Field(GraphQLInt, resolver=_attr("num_pages")),
            "artDirection": Field(GraphQLString, resolver=_attr("art_direction")),
            "coverUrl": Field(
                GraphQLString,
                resolver=lambda s, a, c, i: c.presign(getattr(s, "cover_key", None)),
            ),
            "createdAt": Field(GraphQLDateTime, resolver=_iso("created_at")),
            "page": Field(
                page_type(),
                args={"pageNumber": Argument(GraphQLNonNull(GraphQLInt))},
                resolver=_resolve_book_page,
                cost=2,
            ),
            "pages": Field(
                GraphQLNonNull(connection_type(page_type())),
                args=_connection_args(),
                resolver=resolve_book_pages,
                cost=2,
                list_cost_multiplier=True,
            ),
            "shots": Field(
                GraphQLNonNull(connection_type(shot_type())),
                args=_connection_args(
                    {"status": Argument(SHOT_STATUS_ENUM, description="Filter by status.")}
                ),
                resolver=resolve_book_shots,
                cost=2,
                list_cost_multiplier=True,
                required_scope="books:read",
            ),
            "scenes": Field(
                GraphQLNonNull(connection_type(scene_type())),
                args=_connection_args(),
                resolver=resolve_book_scenes,
                cost=2,
                list_cost_multiplier=True,
            ),
            "canon": Field(
                canon_type(),
                resolver=resolve_book_canon,
                cost=3,
                required_scope="canon:read",
                description="The book's canon graph (entities + continuity + vault).",
            ),
            "directingStyle": Field(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(directing_prior_type()))),
                resolver=_resolve_book_directing_style,
                cost=2,
                required_scope="prefs:read",
                description="The directing priors learned for this book (§8.6).",
            ),
        },
        interfaces=[NODE_INTERFACE],
        description="A book on the shelf and the root of its generated film (§5.1).",
    )


def directing_prior_type() -> GraphQLObject:
    return _cache("DirectingPrior", _build_directing_prior)


def _build_directing_prior() -> GraphQLObject:
    return GraphQLObject(
        "DirectingPrior",
        {
            "kind": Field(GraphQLNonNull(GraphQLString), resolver=_key("kind")),
            "bias": Field(GraphQLNonNull(GraphQLFloat), resolver=_key("bias")),
            "weight": Field(GraphQLNonNull(GraphQLFloat), resolver=_key("weight")),
            "label": Field(GraphQLNonNull(GraphQLString), resolver=_key("label")),
            "detail": Field(GraphQLNonNull(GraphQLString), resolver=_key("detail")),
            "applied": Field(GraphQLNonNull(GraphQLBoolean), resolver=_key("applied")),
            "appliedValue": Field(GraphQLString, resolver=_key("applied_value")),
            "lastNote": Field(GraphQLString, resolver=_key("last_note")),
        },
        description="One learned directing prior, in plain language (§8.6).",
    )


async def _resolve_book_directing_style(
    source: Any, args: dict[str, Any], ctx: Any, info: Any
) -> Any:
    from app.graphql.resolvers.book import resolve_book_directing_style

    return await resolve_book_directing_style(source, args, ctx, info)


async def _resolve_book_page(source: Any, args: dict[str, Any], ctx: Any, info: Any) -> Any:
    from app.graphql.resolvers.book import load_book_page

    return await load_book_page(ctx, source.id, int(args["pageNumber"]))


# --------------------------------------------------------------------------- #
# Session — the generation-on-scroll control surface (read-only here)
# --------------------------------------------------------------------------- #


def session_type() -> GraphQLObject:
    return _cache("Session", _build_session)


def _build_session() -> GraphQLObject:
    from app.graphql.resolvers.session import resolve_session_book

    return GraphQLObject(
        "Session",
        lambda: {
            "id": Field(GraphQLNonNull(GraphQLID), resolver=_node_id("Session", "id")),
            "bookId": Field(GraphQLNonNull(GraphQLID), resolver=_attr("book_id")),
            "focusWord": Field(GraphQLNonNull(GraphQLInt), resolver=_attr("focus_word")),
            "velocityWps": Field(
                GraphQLNonNull(GraphQLFloat), resolver=_attr("velocity_wps")
            ),
            "mode": Field(GraphQLNonNull(SESSION_MODE_ENUM), resolver=_enum_attr("mode")),
            "committedSecondsAhead": Field(
                GraphQLNonNull(GraphQLFloat),
                resolver=lambda s, a, c, i: getattr(s, "committed_seconds_ahead", 0.0) or 0.0,
            ),
            "budgetRemainingSeconds": Field(
                GraphQLFloat, resolver=_attr("budget_remaining_s")
            ),
            "inflight": Field(
                GraphQLJSON,
                resolver=lambda s, a, c, i: getattr(s, "inflight", None) or {},
                description="In-flight render job ids per lane (committed/speculative).",
            ),
            "createdAt": Field(GraphQLDateTime, resolver=_iso("created_at")),
            "book": Field(
                book_type(),
                resolver=resolve_session_book,
                cost=2,
                description="The book this session is reading (dataloader-batched).",
            ),
        },
        interfaces=[NODE_INTERFACE],
        description="A reading session and its live scheduler/control state (§4.9).",
    )


# --------------------------------------------------------------------------- #
# Field-resolver helpers
# --------------------------------------------------------------------------- #


def _attr(name: str) -> Any:
    return lambda s, a, c, i: getattr(s, name, None)


def _key(name: str) -> Any:
    return lambda s, a, c, i: (s.get(name) if isinstance(s, dict) else getattr(s, name, None))


def _enum_attr(name: str) -> Any:
    def resolve(s: Any, a: dict[str, Any], c: Any, i: Any) -> Any:
        value = getattr(s, name, None)
        return getattr(value, "value", value)

    return resolve


def _iso(name: str) -> Any:
    def resolve(s: Any, a: dict[str, Any], c: Any, i: Any) -> Any:
        value = getattr(s, name, None)
        return value.isoformat() if value is not None and hasattr(value, "isoformat") else value

    return resolve


def _node_id(type_name: str, attr: str) -> Any:
    return lambda s, a, c, i: global_id(type_name, str(getattr(s, attr, "")))


# --------------------------------------------------------------------------- #
# Type cache
# --------------------------------------------------------------------------- #

_CACHE: dict[str, GraphQLObject] = {}


def _cache(name: str, builder: Any) -> GraphQLObject:
    if name not in _CACHE:
        _CACHE[name] = builder()
    return _CACHE[name]


__all__ = [
    "book_type",
    "canon_entity_type",
    "canon_state_type",
    "canon_type",
    "page_type",
    "scene_type",
    "session_type",
    "shot_type",
]
