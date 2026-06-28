"""Resolvers for ``Shot`` and the top-level ``shot(id:)`` lookup.

A shot is reachable only within an owned book: ``Query.shot`` loads the shot then
verifies its book belongs to the requesting key's owner (fail-closed). The
``clipUrl`` field presigns the stored clip key (matching
``app/api/routes/books.py``'s projection), and ``Shot.book`` dataloader-batches
its parent book lookup to avoid N+1 across a shot list.
"""

from __future__ import annotations

from typing import Any

from app.db.repositories.shot import ShotRepo
from app.graphql.context import GraphQLContext
from app.graphql.errors import not_found
from app.graphql.execute import ResolveInfo
from app.graphql.resolvers.book import load_book


async def resolve_shot(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.shot(id:)`` — one shot inside an owned book, else ``NOT_FOUND``."""
    ctx.require("books:read")
    async with ctx.container.session_factory() as session:
        shot = await ShotRepo(session).get(str(args["id"]))
    if shot is None:
        raise not_found("No such shot.")
    # Ownership boundary: the shot's book must belong to the caller.
    await load_book(ctx, shot.book_id)
    return shot


def resolve_shot_clip_url(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> str | None:
    """``Shot.clipUrl`` — presign the rendered clip key, if any (mirrors REST)."""
    output: dict[str, Any] = getattr(source, "output", None) or {}
    if output.get("clip_url"):
        return str(output["clip_url"])
    clip_key = output.get("clip_key")
    return ctx.presign(clip_key) if clip_key else None


async def resolve_shot_book(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Shot.book`` — the parent book (dataloader-batched, ownership-checked)."""
    return await ctx.loader("book").load(source.book_id)


__all__ = ["resolve_shot", "resolve_shot_book", "resolve_shot_clip_url"]
