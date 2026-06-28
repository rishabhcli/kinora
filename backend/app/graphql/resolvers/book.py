"""Resolvers for ``Book`` and its relationships (pages, shots, scenes, canon).

All reads go through the same repositories the REST routes use and enforce the
API key owner as the ownership boundary (a book not owned by ``ctx.user_id`` is
``NOT_FOUND``, fail-closed — mirrors ``app/api/routes/books.py``). List
relationships return Relay connections (``app/graphql/pagination.py``); the
canon read reuses the REST projection helpers so the public shape matches the
REST contract.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models.enums import ShotStatus
from app.db.models.scene import Scene
from app.db.models.shot import Shot
from app.db.repositories.book import BookRepo, PageRepo
from app.graphql.context import GraphQLContext
from app.graphql.errors import not_found
from app.graphql.execute import ResolveInfo
from app.graphql.pagination import connection_from_list

#: Resolve canon "as of the latest version" — a beat beyond any real one, so the
#: still-open (current) version of every entity is returned (mirrors REST books.py).
_LATEST_BEAT = 2**31 - 1


async def load_book(ctx: GraphQLContext, book_id: str) -> Any:
    """Load a book the requesting key's owner owns, or raise ``NOT_FOUND``."""
    book = await ctx.loader("book").load(book_id)
    if book is None:
        raise not_found("No such book.")
    return book


async def resolve_book(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.book(id:)`` — fetch one owned book."""
    ctx.require("books:read")
    return await load_book(ctx, str(args["id"]))


async def resolve_books(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.books`` — the requesting owner's shelf as a connection."""
    ctx.require("books:read")
    status = args.get("status")
    async with ctx.container.session_factory() as session:
        books = await BookRepo(session).list_for_user(ctx.user_id)
    if status is not None:
        books = [b for b in books if b.status.value == status]
    # Prime the book loader so a later Shot.book / node refetch is free.
    loader = ctx.loader("book")
    for b in books:
        loader.prime(b.id, b)
    return connection_from_list(
        books,
        first=args.get("first"),
        after=args.get("after"),
        last=args.get("last"),
        before=args.get("before"),
    )


async def load_book_page(ctx: GraphQLContext, book_id: str, page_number: int) -> Any:
    await load_book(ctx, book_id)
    async with ctx.container.session_factory() as session:
        return await PageRepo(session).get_by_number(book_id, page_number)


async def resolve_book_pages(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Book.pages`` — the book's pages in reading order, as a connection."""
    async with ctx.container.session_factory() as session:
        pages = await PageRepo(session).list_for_book(source.id)
    return connection_from_list(
        pages,
        first=args.get("first"),
        after=args.get("after"),
        last=args.get("last"),
        before=args.get("before"),
    )


async def resolve_book_shots(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Book.shots`` — the shot timeline (optionally filtered by status)."""
    ctx.require("books:read")
    status = args.get("status")
    stmt = (
        select(Shot)
        .where(Shot.book_id == source.id)
        .order_by(Shot.scene_id, Shot.beat_id, Shot.id)
    )
    if status is not None:
        stmt = stmt.where(Shot.status == ShotStatus(status))
    async with ctx.container.session_factory() as session:
        rows = list((await session.execute(stmt)).scalars().all())
    return connection_from_list(
        rows,
        first=args.get("first"),
        after=args.get("after"),
        last=args.get("last"),
        before=args.get("before"),
    )


async def resolve_book_scenes(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Book.scenes`` — the book's scenes in narrative order, as a connection."""
    async with ctx.container.session_factory() as session:
        scenes = list(
            (
                await session.execute(
                    select(Scene)
                    .where(Scene.book_id == source.id)
                    .order_by(Scene.scene_index)
                )
            )
            .scalars()
            .all()
        )
    return connection_from_list(
        scenes,
        first=args.get("first"),
        after=args.get("after"),
        last=args.get("last"),
        before=args.get("before"),
    )


async def resolve_book_canon(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Book.canon`` — entities + continuity facts + the vault markdown (§8)."""
    ctx.require("canon:read")
    from app.db.repositories.continuity import ContinuityStateRepo
    from app.db.repositories.entity import EntityRepo
    from app.memory.canon_vault import CanonVault

    container = ctx.container
    async with container.session_factory() as session:
        entities = await EntityRepo(session).list_active_at_beat(source.id, _LATEST_BEAT)
        entity_views = [_canon_entity_view(container, e) for e in entities]
        states = await ContinuityStateRepo(session).list_for_book(source.id)
        state_views = [_canon_state_view(s) for s in states]
        export = await CanonVault(session, blob_store=container.object_store).export(source.id)
    markdown = "\n\n".join(export.files.values()) or None
    return {
        "book_id": source.id,
        "entities": entity_views,
        "states": state_views,
        "markdown": markdown,
    }


def _canon_entity_view(container: Any, entity: Any) -> dict[str, Any]:
    """Project a canon entity row into the public ``CanonEntity`` shape (presigned)."""
    appearance = _presign_appearance(container, entity.appearance or {})
    return {
        "id": entity.entity_key,
        "type": entity.type.value,
        "name": entity.name,
        "aliases": list(entity.aliases or []),
        "description": entity.description,
        "appearance": appearance,
        "style_tokens": entity.style_tokens,
        "voice": entity.voice,
        "version": entity.version,
        "valid_from_beat": entity.valid_from_beat,
        "valid_to_beat": entity.valid_to_beat,
        "__typename": "CanonEntity",
    }


def _presign_appearance(container: Any, appearance: dict[str, Any]) -> dict[str, Any] | None:
    if not appearance:
        return None
    out = dict(appearance)
    refs = appearance.get("reference_images")
    if isinstance(refs, list):
        presigned = []
        for item in refs:
            if isinstance(item, dict):
                key = item.get("key") or item.get("oss_key")
                presigned.append(
                    {
                        **item,
                        "oss_url": (
                            container.object_store.presigned_get_url(key) if key else None
                        ),
                    }
                )
        out["reference_images"] = presigned
    return out


async def resolve_book_directing_style(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> list[dict[str, Any]]:
    """``Book.directingStyle`` — the priors learned for this book (§8.6).

    Projects the aggregated :class:`PreferencePriors` into the same plain-language
    rows the REST ``/books/{id}/prefs`` panel uses, so the public field matches the
    REST contract.
    """
    ctx.require("prefs:read")
    await load_book(ctx, source.id)
    from app.memory.prefs_signals import (
        AXIS_KINDS,
        applied_value,
        bias_of,
        describe,
        is_applied,
    )

    priors = await ctx.container.get_prefs(book_id=source.id)
    ordered = [*AXIS_KINDS, *(k for k in priors.priors if k not in AXIS_KINDS)]
    out: list[dict[str, Any]] = []
    for kind in ordered:
        prior = priors.priors.get(kind)
        if prior is None:
            continue
        label, detail = describe(prior)
        note = prior.value.get("note") if isinstance(prior.value, dict) else None
        out.append(
            {
                "kind": prior.kind,
                "bias": bias_of(prior),
                "weight": prior.weight,
                "label": label,
                "detail": detail,
                "applied": is_applied(prior),
                "applied_value": applied_value(prior),
                "last_note": note if isinstance(note, str) else None,
            }
        )
    return out


def _canon_state_view(state: Any) -> dict[str, Any]:
    return {
        "id": state.id,
        "subject_entity_key": state.subject_entity_key,
        "predicate": state.predicate,
        "object_value": state.object_value,
        "valid_from_beat": state.valid_from_beat,
        "valid_to_beat": state.valid_to_beat,
        "version": state.version,
        "active": state.valid_to_beat is None,
        "source_span": state.source_span,
    }


__all__ = [
    "load_book",
    "load_book_page",
    "resolve_book",
    "resolve_book_canon",
    "resolve_book_directing_style",
    "resolve_book_pages",
    "resolve_book_scenes",
    "resolve_book_shots",
    "resolve_books",
]
