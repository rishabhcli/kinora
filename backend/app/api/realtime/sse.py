"""Server-Sent Events framing + a reusable resumable stream (kinora.md §5.6).

Round-1's :mod:`app.api.routes.events` hand-rolls a minimal SSE frame
(``event:``/``data:`` only) and a keepalive comment. This module supplies the
full, spec-correct framing the resumable transport needs:

* an ``id:`` line on every data event (the cursor the client echoes back as
  ``Last-Event-ID``),
* a ``retry:`` line so the browser uses *our* reconnect backoff, not its 3s
  default,
* multi-line ``data:`` handling (each ``\\n`` in the payload becomes its own
  ``data:`` line, per the EventSource grammar — a raw embedded newline silently
  truncates the event otherwise),
* heartbeat comments (`: ping`) to keep intermediaries from idling the socket.

It also provides :class:`EventStream`, the generator the route mounts: it
**replays** the per-session :class:`~app.api.realtime.event_log.EventLog` after a
``Last-Event-ID`` and then **tails** the live Redis pub/sub channels, bounded by
a maximum lifetime (so a forgotten tab can't pin a worker forever) and a
disconnect poll. The replay→tail handoff is the whole point: a reconnecting
client gets the gap events first, in order, then continues live with no
duplicates and no holes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.api.realtime.event_log import EventLog
from app.core.logging import get_logger

logger = get_logger("app.api.realtime.sse")

#: Default client reconnect backoff advertised via the ``retry:`` line (ms).
DEFAULT_RETRY_MS = 3000
#: Heartbeat cadence — a comment line keeps proxies/load balancers from idling
#: the connection. Matches the round-1 KEEPALIVE_S.
DEFAULT_HEARTBEAT_S = 15.0
#: Hard cap on a single SSE connection's lifetime. A browser EventSource
#: transparently reconnects (resuming via Last-Event-ID), so cycling the socket
#: every ~30 min frees server resources and rebalances across replicas without
#: the reader ever noticing. 0 disables the cap.
DEFAULT_MAX_LIFETIME_S = 1800.0


def format_comment(text: str) -> str:
    """An SSE comment line (ignored by clients; used for heartbeats)."""
    return f": {text}\n\n"


def format_retry(retry_ms: int) -> str:
    """A ``retry:`` directive setting the client's reconnect backoff."""
    return f"retry: {retry_ms}\n\n"


def format_event(
    payload: dict[str, Any],
    *,
    event: str | None = None,
    event_id: int | str | None = None,
) -> str:
    """Render a fully-framed SSE event.

    ``event`` defaults to ``payload["event"]`` (the §5.6 event name); ``event_id``
    becomes the ``id:`` line clients echo back on reconnect. The JSON ``data`` is
    split across ``data:`` lines so an embedded newline can't truncate the frame.
    """
    name = event if event is not None else str(payload.get("event", "message"))
    body = json.dumps(payload, separators=(",", ":"))
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    for chunk in body.split("\n"):
        lines.append(f"data: {chunk}")
    return "\n".join(lines) + "\n\n"


@dataclass(slots=True)
class StreamConfig:
    """Tunables for an :class:`EventStream` (overridable per route / in tests)."""

    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    retry_ms: int = DEFAULT_RETRY_MS
    max_lifetime_s: float = DEFAULT_MAX_LIFETIME_S
    replay_limit: int = 1024


class EventStream:
    """A resumable SSE generator: replay-after-cursor, then live-tail.

    The route supplies a ``subscribe`` factory (an ``async with`` over the Redis
    pub/sub channels) and a ``next_message`` reader; this class owns the framing,
    the cursor, the heartbeat, the lifetime cap, and the disconnect poll. It is
    transport-agnostic about *what* it subscribes to, so the same machine serves
    the per-session stream and the per-user library stream.
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        log_stream: str,
        subscribe: Callable[[], Any],
        next_message: Callable[[Any, float], Awaitable[dict[str, Any] | None]],
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        last_event_id: int | None = None,
        config: StreamConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = event_log
        self._log_stream = log_stream
        self._subscribe = subscribe
        self._next_message = next_message
        self._is_disconnected = is_disconnected
        self._last_id = last_event_id
        self._cfg = config or StreamConfig()
        self._clock = clock

    async def _disconnected(self) -> bool:
        if self._is_disconnected is None:
            return False
        with contextlib.suppress(Exception):
            return await self._is_disconnected()
        return False

    async def iter_frames(self) -> AsyncIterator[str]:
        """Yield SSE frames: the handshake, any replay, then the live tail."""
        # 1. Advertise our reconnect backoff up-front.
        yield format_retry(self._cfg.retry_ms)

        cursor = self._last_id
        # 2. Replay the gap if the client is resuming.
        if cursor is not None:
            result = await self._log.replay_after(
                self._log_stream, cursor, limit=self._cfg.replay_limit
            )
            if result.gap:
                # The client's last id predates retention — tell it to re-seed.
                yield format_event(
                    {"event": "resume_gap", "from_id": cursor, "oldest_retained": result.last_id},
                    event="resume_gap",
                )
            for logged in result.events:
                yield format_event(logged.payload, event_id=logged.id)
                cursor = logged.id

        # 3. Handshake comment so clients/tests know the subscription is live.
        yield format_comment("connected")

        deadline = (
            self._clock() + self._cfg.max_lifetime_s if self._cfg.max_lifetime_s > 0 else None
        )
        async with self._subscribe() as pubsub:
            while True:
                if await self._disconnected():
                    break
                if deadline is not None and self._clock() >= deadline:
                    # Graceful lifetime rollover — the client reconnects + resumes.
                    yield format_event(
                        {"event": "stream_cycle", "reason": "max_lifetime"},
                        event="stream_cycle",
                    )
                    break
                message = await self._next_message(pubsub, self._cfg.heartbeat_s)
                if message is None:
                    yield format_comment("ping")
                    continue
                if not isinstance(message, dict):
                    continue
                event_id = message.get("id")
                yield format_event(message, event_id=event_id)
        logger.info("sse.stream_closed", stream=self._log_stream)


def parse_last_event_id(headers: Any, query_value: str | None = None) -> int | None:
    """Resolve a numeric ``Last-Event-ID`` from headers (or a ``?last_event_id``).

    EventSource sends the header automatically on reconnect; a WebSocket client
    (which can't set arbitrary reconnect headers) passes a query param instead.
    A non-numeric value yields ``None`` (treated as a fresh, non-resuming
    connection) rather than raising.
    """
    raw: str | None = None
    getter = getattr(headers, "get", None)
    if getter is not None:
        raw = headers.get("last-event-id") or headers.get("Last-Event-ID")
    if raw is None:
        raw = query_value
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def drain(stream: AsyncIterator[str]) -> Sequence[str]:  # pragma: no cover - test helper
    """Collect all frames from a finite stream (used by tests)."""
    out: list[str] = []
    async for frame in stream:
        out.append(frame)
    return out


async def _noop_disconnected() -> bool:  # pragma: no cover - default sentinel
    return False


# Re-exported so callers can build a never-disconnect probe trivially in tests.
async def never_disconnected() -> bool:  # pragma: no cover - trivial
    """A disconnect probe that always reports 'still connected'."""
    await asyncio.sleep(0)
    return False


__all__ = [
    "DEFAULT_HEARTBEAT_S",
    "DEFAULT_MAX_LIFETIME_S",
    "DEFAULT_RETRY_MS",
    "EventStream",
    "StreamConfig",
    "format_comment",
    "format_event",
    "format_retry",
    "never_disconnected",
    "parse_last_event_id",
]
