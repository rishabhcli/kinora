"""Resolver for ``Scene.shots`` — the accepted shots composing a scene's film.

Returns the scene's accepted shots in narrative order (beat index then source
word, matching the §9.6 stitch order used by ``app/api/routes/films.py``), as a
Relay connection.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models.beat import Beat
from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.scene import SceneRepo
from app.graphql.context import GraphQLContext
from app.graphql.errors import not_found
from app.graphql.execute import ResolveInfo
from app.graphql.pagination import connection_from_list
from app.graphql.resolvers.book import load_book


async def resolve_scene(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.scene(id:)`` — one scene inside an owned book, else ``NOT_FOUND``."""
    ctx.require("books:read")
    async with ctx.container.session_factory() as session:
        scene = await SceneRepo(session).get(str(args["id"]))
    if scene is None:
        raise not_found("No such scene.")
    await load_book(ctx, scene.book_id)  # ownership boundary
    return scene


def _span_start(shot: Shot) -> int:
    raw = (shot.source_span or {}).get("word_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return int(raw[0])
    return 0


async def resolve_scene_shots(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Scene.shots`` — accepted shots in stitch order (§9.6) as a connection."""
    stmt = (
        select(Shot, Beat.beat_index)
        .join(Beat, Beat.id == Shot.beat_id, isouter=True)
        .where(Shot.scene_id == source.id, Shot.status == ShotStatus.ACCEPTED)
    )
    async with ctx.container.session_factory() as session:
        rows = list((await session.execute(stmt)).all())
    rows.sort(key=lambda p: (p[1] is None, p[1] if p[1] is not None else 0, _span_start(p[0])))
    shots = [shot for shot, _ in rows]
    return connection_from_list(
        shots,
        first=args.get("first"),
        after=args.get("after"),
        last=args.get("last"),
        before=args.get("before"),
    )


__all__ = ["resolve_scene", "resolve_scene_shots"]
