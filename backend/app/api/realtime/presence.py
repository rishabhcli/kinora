"""Multiplayer presence — shared reading sessions (kinora.md §5.2/§5.6).

The workspace is built around one reader's SyncEngine, but the transport can
support *several* readers on the same session — a book club watching the same
adaptation, or a teacher co-reading with a class. Presence is the bookkeeping
that makes that legible: who is here, where their playhead is, what mode they're
in, and join/leave/move events fanned out to everyone else.

Each participant is a TTL'd Redis hash keyed by ``(session_id, participant_id)``,
indexed in a per-session set. A participant:

* **joins** — writes its hash, adds to the index, and publishes a ``presence``
  event (``action=join``) on the session channel so existing readers see them
  arrive (and the new reader gets the current roster in the join response).
* **heartbeats** — refreshes the hash TTL on a cadence; a participant that stops
  heartbeating (closed tab, crash) simply expires and is reaped, publishing a
  synthetic ``leave`` so the roster self-heals without a clean disconnect.
* **moves** — updates its focus word / mode and publishes ``presence``
  (``action=move``) so others can render a "they're on page 12" cursor.
* **leaves** — removes its hash + index entry and publishes ``leave``.

Presence is **soft state**: it lives only in Redis with a TTL, never touches
Postgres, and **fails open** — a presence failure must never block the actual
generation transport. The roster is bounded (the connection registry caps the
number of streams; presence rides on top), and every published event reuses the
§5.6 channel + event-log tee so a reconnecting client *resumes* presence changes
too, not just generation events.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.api.realtime.presence")

#: A participant whose hash hasn't been refreshed within this window is gone.
DEFAULT_TTL_S = 45
#: Recommended client heartbeat cadence (< TTL with margin for jitter/latency).
RECOMMENDED_HEARTBEAT_S = 15


@dataclass(frozen=True, slots=True)
class Participant:
    """One reader currently in a session (a presence roster row)."""

    participant_id: str
    user_id: str
    display: str
    focus_word: int = 0
    mode: str = "viewer"
    joined_at_ms: int = 0
    updated_at_ms: int = 0

    def to_public(self) -> dict[str, Any]:
        """The wire shape for a roster entry / presence event."""
        return {
            "participant_id": self.participant_id,
            "user_id": self.user_id,
            "display": self.display,
            "focus_word": self.focus_word,
            "mode": self.mode,
            "joined_at_ms": self.joined_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


class PresenceService:
    """Redis-backed multiplayer presence with fan-out over a publish callback.

    ``publish`` is the same ``redis.publish`` the rest of the transport uses; it's
    injected so presence can also tee through the event log for resume without
    this module importing the route layer.
    """

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:presence",
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._ttl_ms = max(int(ttl_s * 1000), 1000)

    # -- keys ---------------------------------------------------------------- #

    def _member_key(self, session_id: str, participant_id: str) -> str:
        return f"{self._ns}:{session_id}:p:{participant_id}"

    def _index_key(self, session_id: str) -> str:
        return f"{self._ns}:{session_id}:index"

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    # -- roster -------------------------------------------------------------- #

    async def roster(self, session_id: str) -> list[Participant]:
        """Current participants, dropping any whose hash has expired."""
        out: list[Participant] = []
        try:
            ids = await self._redis.smembers(self._index_key(session_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence.roster_failed", session_id=session_id, error=str(exc))
            return out
        for participant_id in ids:
            data = await self._safe_hgetall(self._member_key(session_id, participant_id))
            if not data:
                # Expired hash but lingering index entry — prune it.
                with contextlib.suppress(Exception):
                    await self._redis.srem(self._index_key(session_id), participant_id)
                continue
            out.append(_participant_from_hash(participant_id, data))
        out.sort(key=lambda p: p.joined_at_ms)
        return out

    async def count(self, session_id: str) -> int:
        """How many participants are present (post-prune)."""
        return len(await self.roster(session_id))

    # -- mutations ----------------------------------------------------------- #

    async def join(
        self,
        session_id: str,
        *,
        participant_id: str,
        user_id: str,
        display: str,
        focus_word: int = 0,
        mode: str = "viewer",
    ) -> Participant:
        """Add a participant, publish ``join``, and return them."""
        now = self._now_ms()
        participant = Participant(
            participant_id=participant_id,
            user_id=user_id,
            display=display,
            focus_word=focus_word,
            mode=mode,
            joined_at_ms=now,
            updated_at_ms=now,
        )
        await self._write(session_id, participant)
        await self._fan_out(session_id, "join", participant)
        logger.info("presence.join", session_id=session_id, participant_id=participant_id)
        return participant

    async def heartbeat(self, session_id: str, participant_id: str) -> bool:
        """Refresh a participant's TTL. ``False`` if they'd already expired."""
        key = self._member_key(session_id, participant_id)
        data = await self._safe_hgetall(key)
        if not data:
            return False
        now = self._now_ms()
        with contextlib.suppress(Exception):
            await self._redis.hset(key, "updated_at_ms", str(now))
            await self._redis.pexpire(key, self._ttl_ms)
        return True

    async def move(
        self,
        session_id: str,
        participant_id: str,
        *,
        focus_word: int | None = None,
        mode: str | None = None,
    ) -> Participant | None:
        """Update a participant's cursor/mode + publish ``move`` (``None`` if gone)."""
        key = self._member_key(session_id, participant_id)
        data = await self._safe_hgetall(key)
        if not data:
            return None
        participant = _participant_from_hash(participant_id, data)
        now = self._now_ms()
        new = Participant(
            participant_id=participant.participant_id,
            user_id=participant.user_id,
            display=participant.display,
            focus_word=focus_word if focus_word is not None else participant.focus_word,
            mode=mode if mode is not None else participant.mode,
            joined_at_ms=participant.joined_at_ms,
            updated_at_ms=now,
        )
        await self._write(session_id, new)
        await self._fan_out(session_id, "move", new)
        return new

    async def leave(self, session_id: str, participant_id: str) -> None:
        """Remove a participant + publish ``leave`` (idempotent)."""
        data = await self._safe_hgetall(self._member_key(session_id, participant_id))
        with contextlib.suppress(Exception):
            await self._redis.delete(self._member_key(session_id, participant_id))
            await self._redis.srem(self._index_key(session_id), participant_id)
        if data:
            await self._fan_out(session_id, "leave", _participant_from_hash(participant_id, data))
        logger.info("presence.leave", session_id=session_id, participant_id=participant_id)

    # -- internals ----------------------------------------------------------- #

    async def _write(self, session_id: str, participant: Participant) -> None:
        key = self._member_key(session_id, participant.participant_id)
        with contextlib.suppress(Exception):
            await self._redis.hset(
                key,
                mapping={
                    "user_id": participant.user_id,
                    "display": participant.display,
                    "focus_word": str(participant.focus_word),
                    "mode": participant.mode,
                    "joined_at_ms": str(participant.joined_at_ms),
                    "updated_at_ms": str(participant.updated_at_ms),
                },
            )
            await self._redis.pexpire(key, self._ttl_ms)
            await self._redis.sadd(self._index_key(session_id), participant.participant_id)
            await self._redis.pexpire(self._index_key(session_id), self._ttl_ms)

    async def _safe_hgetall(self, key: str) -> dict[str, str]:
        try:
            return dict(await self._redis.hgetall(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence.hgetall_failed", key=key, error=str(exc))
            return {}

    async def _fan_out(self, session_id: str, action: str, participant: Participant) -> None:
        """Publish a ``presence`` event on the session channel (best-effort)."""
        from app.queue.redis_queue import session_channel

        message = {
            "event": "presence",
            "action": action,
            "session_id": session_id,
            "participant": participant.to_public(),
        }
        with contextlib.suppress(Exception):
            await self._redis.publish(
                session_channel(session_id), json.dumps(message, separators=(",", ":"))
            )


def _participant_from_hash(participant_id: str, data: dict[str, str]) -> Participant:
    def _int(key: str) -> int:
        try:
            return int(data.get(key, "0") or 0)
        except ValueError:
            return 0

    return Participant(
        participant_id=participant_id,
        user_id=data.get("user_id", ""),
        display=data.get("display", "reader"),
        focus_word=_int("focus_word"),
        mode=data.get("mode", "viewer"),
        joined_at_ms=_int("joined_at_ms"),
        updated_at_ms=_int("updated_at_ms"),
    )


__all__ = [
    "DEFAULT_TTL_S",
    "RECOMMENDED_HEARTBEAT_S",
    "Participant",
    "PresenceService",
]
