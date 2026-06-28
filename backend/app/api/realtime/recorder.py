"""The event recorder — a fan-in tee that makes every §5.6 event resumable.

Round-1's publishers (the Scheduler, the render worker, the Director routes, the
keyframe lane) fire §5.6 events onto per-session Redis pub/sub channels
(``kinora:events:session:{id}``). Pub/sub is fire-and-forget, so those events
vanish the instant they're delivered — there is nothing to replay after a
reconnect.

The recorder is the single, replica-safe component that fixes that **without
touching any publisher**. It pattern-subscribes (``PSUBSCRIBE
kinora:events:session:*``) to *all* session channels and appends every message
to that session's :class:`~app.api.realtime.event_log.EventLog` exactly once.
Because exactly one recorder instance per replica records each event, and the log
assigns ids server-side, the cursor a client echoes back on reconnect is
globally consistent regardless of how many clients are streaming.

Why a recorder and not per-stream logging? If each SSE connection logged the
events it received, two co-readers on one session would each append the same
event under different ids — duplicating storage and corrupting the cursor. The
recorder centralises the append so the log is the *system's* record, not a
per-connection one. It is the realtime analogue of the render queue's Postgres
mirror: a durable shadow of an otherwise-ephemeral stream.

Operationally it is a single ``asyncio`` task started in the app lifespan, fully
defensive (a decode error or a Redis blip is logged and skipped, never fatal),
and stoppable for a clean shutdown.

> **Multi-replica note.** Running N API replicas means N recorders, each
> appending every event — so an event would get N ids. In a single-process dev /
> demo deployment (the project's posture) there is exactly one recorder and the
> ids are clean. To stay correct under horizontal scale, enable
> ``elect_leader=True`` so only the recorder that holds a short-lived Redis lease
> records; the others stand by and take over if the leader dies. The lease is the
> same ``SET NX PX`` primitive the queue/locks already use.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from app.api.realtime.event_log import EventLog
from app.core.logging import get_logger

logger = get_logger("app.api.realtime.recorder")

#: The pub/sub pattern matching every per-session event channel.
SESSION_CHANNEL_PATTERN = "kinora:events:session:*"
#: Prefix stripped to recover the session id from a matched channel name.
_SESSION_PREFIX = "kinora:events:session:"
#: Leader lease key + TTL when ``elect_leader`` is on (horizontal-scale safety).
_LEADER_KEY = "kinora:evlog:recorder:leader"
_LEADER_TTL_MS = 8_000
_LEADER_RENEW_S = 3.0


def session_id_from_channel(channel: str) -> str | None:
    """Recover the session id from a ``kinora:events:session:{id}`` channel name."""
    if channel.startswith(_SESSION_PREFIX):
        sid = channel[len(_SESSION_PREFIX) :]
        return sid or None
    return None


class EventRecorder:
    """Pattern-subscribes to session channels and tees every event into the log."""

    def __init__(
        self,
        redis: Any,
        event_log: EventLog,
        *,
        pattern: str = SESSION_CHANNEL_PATTERN,
        elect_leader: bool = False,
    ) -> None:
        self._redis: Any = getattr(redis, "raw", redis)
        self._log = event_log
        self._pattern = pattern
        self._elect_leader = elect_leader
        self._leader_token = f"{id(self):x}:{time.time_ns():x}"
        self._recorded = 0

    @property
    def recorded(self) -> int:
        """Lifetime count of events this recorder has appended (diagnostics)."""
        return self._recorded

    async def _is_leader(self) -> bool:
        """Acquire/renew the recorder lease; ``True`` iff we hold it."""
        if not self._elect_leader:
            return True
        try:
            got = await self._redis.set(
                _LEADER_KEY, self._leader_token, nx=True, px=_LEADER_TTL_MS
            )
            if got:
                return True
            current = await self._redis.get(_LEADER_KEY)
            if current == self._leader_token:
                # We already hold it — renew the lease.
                await self._redis.pexpire(_LEADER_KEY, _LEADER_TTL_MS)
                return True
        except Exception as exc:  # noqa: BLE001 - never crash on a lease blip
            logger.warning("recorder.leader_check_failed", error=str(exc))
        return False

    async def _record(self, channel: str, payload: dict[str, Any]) -> None:
        session_id = session_id_from_channel(channel)
        if session_id is None:
            return
        # An event that already carries an id was framed by a prior recorder pass
        # (or re-published); don't double-log it.
        if "id" in payload:
            return
        assigned = await self._log.append(session_id, payload)
        if assigned is not None:
            self._recorded += 1

    async def run(self, stop: asyncio.Event) -> None:
        """Run the recorder loop until ``stop`` is set (the lifespan task body)."""
        logger.info("recorder.start", pattern=self._pattern, elect_leader=self._elect_leader)
        last_renew = 0.0
        while not stop.is_set():
            try:
                await self._run_subscription(stop, last_renew)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any pubsub error
                logger.warning("recorder.subscription_error", error=str(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
        logger.info("recorder.stop", recorded=self._recorded)

    async def _run_subscription(self, stop: asyncio.Event, last_renew: float) -> None:
        pubsub: Any = self._redis.pubsub()
        await pubsub.psubscribe(self._pattern)
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self._elect_leader and now - last_renew >= _LEADER_RENEW_S:
                    last_renew = now
                    if not await self._is_leader():
                        # A non-leader idles (still subscribed so it can take over
                        # instantly), but does not record.
                        await self._drain_once(pubsub, record=False)
                        continue
                await self._drain_once(pubsub, record=True)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.punsubscribe(self._pattern)
                await pubsub.aclose()

    async def _drain_once(self, pubsub: Any, *, record: bool) -> None:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if not message or message.get("type") != "pmessage":
            return
        if not record:
            return
        channel = _as_str(message.get("channel"))
        payload = _decode(message.get("data"))
        if channel and isinstance(payload, dict):
            await self._record(channel, payload)


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value) if value is not None else ""


def _decode(data: Any) -> Any:
    if data is None:
        return None
    raw = data.decode("utf-8", "ignore") if isinstance(data, bytes) else data
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


__all__ = [
    "SESSION_CHANNEL_PATTERN",
    "EventRecorder",
    "session_id_from_channel",
]
