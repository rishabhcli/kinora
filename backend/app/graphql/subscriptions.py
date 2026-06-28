"""The subscription bridge: §5.6 generation events → GraphQL subscription frames.

GraphQL subscriptions over plain HTTP are delivered as Server-Sent Events here
(the same transport the REST ``/sessions/{id}/events`` route uses). A
``subscription { sessionEvents(sessionId:) }`` operation is validated + scoped
like any operation, then this module subscribes to the session's (and its book's)
Redis pub/sub channels and emits one ``data:`` frame per event in the
GraphQL-over-SSE shape ``{"data": {"<responseKey>": <event>}}``.

The bridge owns *only* the streaming; it does not modify the existing realtime
routes (it subscribes to the same channels additively).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from app.graphql.errors import GraphQLError
from app.graphql.language import parse
from app.graphql.language.ast import Field, OperationDefinition
from app.graphql.validate import validate

if TYPE_CHECKING:
    from app.graphql.context import GraphQLContext
    from app.graphql.schema import Schema

#: SSE keepalive cadence (seconds) — mirrors the REST events route.
KEEPALIVE_S = 15.0


def parse_subscription(
    schema: Schema, query: str, operation_name: str | None
) -> tuple[OperationDefinition, Field]:
    """Validate a subscription document and return its single root field.

    A subscription must define exactly one root field (the spec's
    single-root-field rule); anything else is a validation error.
    """
    document = parse(query)
    errors = validate(schema, document)
    if errors:
        raise errors[0]
    from app.graphql.execute import select_operation

    operation = select_operation(document, operation_name)
    if operation.operation != "subscription":
        raise GraphQLError("Expected a subscription operation.")
    fields = [s for s in operation.selection_set.selections if isinstance(s, Field)]
    if len(fields) != 1:
        raise GraphQLError("A subscription must select exactly one root field.")
    return operation, fields[0]


async def stream_session_events(
    ctx: GraphQLContext,
    *,
    session_id: str,
    response_key: str,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames of a session's §5.6 events as GraphQL subscription data.

    Enforces the ``sessions:read`` scope + the owner-boundary before subscribing,
    then forwards each Redis event as a GraphQL-over-SSE ``data:`` frame.
    """
    from app.graphql.resolvers.session import load_session_row
    from app.queue.redis_queue import book_channel, session_channel

    ctx.require("sessions:read")
    row = await load_session_row(ctx, session_id)
    channels = [session_channel(session_id), book_channel(row.book_id)]
    redis = ctx.container.redis

    async with redis.subscribe(*channels) as pubsub:
        yield ": connected\n\n"
        while True:
            message = await redis.next_message(pubsub, timeout=KEEPALIVE_S)
            if message is None:
                yield ": keepalive\n\n"
                continue
            if isinstance(message, dict):
                frame = {"data": {response_key: message}}
                yield f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"


async def collect_session_events(
    ctx: GraphQLContext, *, session_id: str, response_key: str, limit: int
) -> list[dict[str, object]]:
    """Drain up to ``limit`` events from a session stream (used by tests).

    A pure helper that consumes :func:`stream_session_events`, parsing the JSON
    ``data:`` frames back into payloads and stopping after ``limit`` or a short
    idle. Keepalive/comment frames are skipped.
    """
    out: list[dict[str, object]] = []
    agen = stream_session_events(ctx, session_id=session_id, response_key=response_key)
    try:
        while len(out) < limit:
            try:
                frame = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
            except (StopAsyncIteration, TimeoutError):
                break
            if frame.startswith("data: "):
                payload = json.loads(frame[len("data: ") :].strip())
                out.append(payload)
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()
    return out


__all__ = [
    "KEEPALIVE_S",
    "collect_session_events",
    "parse_subscription",
    "stream_session_events",
]
