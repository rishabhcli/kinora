"""Resolvers for ``Session`` reads + the ``session(id:)`` lookup.

A session is owned by the requesting key's user (fail-closed: a session with a
different/NULL owner is ``NOT_FOUND``, mirroring ``app/api/routes/sessions.py``).
The read returns the durable ``sessions`` row enriched with the Scheduler's live
control state when present.
"""

from __future__ import annotations

from typing import Any

from app.db.repositories.session import SessionRepo
from app.graphql.context import GraphQLContext
from app.graphql.errors import not_found
from app.graphql.execute import ResolveInfo


async def load_session_row(ctx: GraphQLContext, session_id: str) -> Any:
    """Load a session row the requesting key's owner owns, or ``NOT_FOUND``."""
    async with ctx.container.session_factory() as session:
        row = await SessionRepo(session).get(session_id)
    if row is None or row.user_id != ctx.user_id:
        raise not_found("No such session.")
    return row


async def resolve_session(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Query.session(id:)`` — one owned session with its live control state."""
    ctx.require("sessions:read")
    row = await load_session_row(ctx, str(args["id"]))
    sched = await ctx.container.scheduler_store.load(row.id)
    if sched is not None:
        # Overlay the live buffer state (committed-seconds-ahead + inflight) from
        # the Scheduler; the durable row stays the source of truth for the reading
        # position (focus_word/velocity), which the Scheduler does not own.
        row.committed_seconds_ahead = sched.committed_seconds_ahead
        row.inflight = sched.inflight
    return row


async def resolve_session_book(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``Session.book`` — the book being read (dataloader-batched, ownership-checked)."""
    return await ctx.loader("book").load(source.book_id)


__all__ = ["load_session_row", "resolve_session", "resolve_session_book"]
