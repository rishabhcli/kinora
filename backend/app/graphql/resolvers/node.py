"""The ``Query.node(id:)`` resolver — refetch any object by its global id.

Decodes the opaque global id into ``(typeName, localId)`` and dispatches to the
matching loader, enforcing the owner-boundary the per-type loaders already apply.
An id naming an unknown type returns ``null`` (a not-found node), per Relay.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.db.repositories.shot import ShotRepo
from app.graphql.context import GraphQLContext
from app.graphql.execute import ResolveInfo
from app.graphql.resolvers.book import load_book
from app.graphql.resolvers.session import load_session_row
from app.graphql.types.node import from_global_id


async def resolve_node(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.node(id:)`` — global object lookup across the owner's data."""
    type_name, local_id = from_global_id(str(args["id"]))
    try:
        if type_name == "Book":
            ctx.require("books:read")
            return _typed(await load_book(ctx, local_id), "Book")
        if type_name == "Session":
            ctx.require("sessions:read")
            return _typed(await load_session_row(ctx, local_id), "Session")
        if type_name == "Shot":
            ctx.require("books:read")
            async with ctx.container.session_factory() as session:
                shot = await ShotRepo(session).get(local_id)
            if shot is None:
                return None
            await load_book(ctx, shot.book_id)  # ownership boundary
            return _typed(shot, "Shot")
        if type_name == "Scene":
            return await _load_scene(ctx, local_id)
        if type_name == "Page":
            return await _load_page(ctx, local_id)
    except Exception:  # noqa: BLE001 - a not-found node resolves to null, not an error
        return None
    return None


async def _load_scene(ctx: GraphQLContext, scene_id: str) -> Any:
    from app.db.repositories.scene import SceneRepo

    async with ctx.container.session_factory() as session:
        scene = await SceneRepo(session).get(scene_id)
    if scene is None:
        return None
    await load_book(ctx, scene.book_id)
    return _typed(scene, "Scene")


async def _load_page(ctx: GraphQLContext, page_id: str) -> Any:
    from app.db.repositories.book import PageRepo

    async with ctx.container.session_factory() as session:
        page = await PageRepo(session).get(page_id)
    if page is None:
        return None
    await load_book(ctx, page.book_id)
    return _typed(page, "Page")


def _typed(value: Any, type_name: str) -> Any:
    """Stamp a ``__typename`` so the Node interface resolves the concrete type."""
    with contextlib.suppress(AttributeError, TypeError):
        value.__typename = type_name
    return value


__all__ = ["resolve_node"]
