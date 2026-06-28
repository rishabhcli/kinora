"""Per-session append-only event log for SSE/WS **resume** (kinora.md §5.6).

The round-1 transport (:mod:`app.api.routes.events`) forwards Redis pub/sub
messages straight to the client. Pub/sub is *fire-and-forget*: if a reader's
connection blips (a tunnel, a sleep/wake, a flaky cell network) every event
published during the gap is gone, and the §5.4 Director loses the ``regen_done``
that swaps a shot, or the §5.3 viewer loses a ``clip_ready`` hot-swap.

This module makes the stream **resumable**. Every event that fans out on a
session channel is *also* appended to a capped per-session log keyed by a
strictly monotonic integer id. An SSE frame carries that id in its ``id:`` line;
the browser's ``EventSource`` echoes the last one it saw back as the
``Last-Event-ID`` request header on automatic reconnect. The server reads that
header, **replays** every logged event after it, and only then tails live — so a
reconnect is seamless and lossless within the retention window.

Design choices:

* **Storage = a Redis sorted set** (``ZADD`` scored by the id) plus an atomic
  ``INCR`` counter for the id. A sorted set gives O(log n) range-after-id reads
  (``ZRANGEBYSCORE (last +inf``) and O(1) trimming of the oldest by rank
  (``ZREMRANGEBYRANK``), which is exactly the capped-ring-buffer shape we want.
  (Redis Streams would also work, but the sorted set keeps us on the same
  ``redis.raw`` surface the rest of the codebase already uses and needs no
  consumer-group bookkeeping.)
* **Append is atomic** via one small Lua script: bump the counter, ZADD, trim to
  ``max_len``, refresh the TTL. No interleaving can mis-order ids or leave the
  log unbounded.
* **Bounded** by both ``max_len`` (ring size) and ``ttl_s`` (a session that goes
  away frees its log). A resume whose ``Last-Event-ID`` predates the retained
  window is detected (the requested id is below the floor) and the caller is told
  to do a full re-seed rather than silently skipping events.
* **Fail-open on append**: a logging failure must never break the live fan-out,
  so :meth:`append` swallows + logs Redis errors. Resume is best-effort; live
  delivery is the contract.

The log is a transparent *tee*: the payload it stores is the very same §5.6
event dict already published on the channel, with an injected ``"id"`` field so a
client that reads either transport sees a consistent cursor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.api.realtime.event_log")

#: Default ring size — how many recent events a session retains for resume.
#: One reading session rarely emits more than a few hundred events between a
#: blip and a reconnect; 512 covers a long Director session comfortably.
DEFAULT_MAX_LEN = 512
#: Default retention — a log outlives a brief disconnect but not an abandoned
#: session. 1h >> any reconnect window, << a forgotten tab.
DEFAULT_TTL_S = 3600

# Atomic append: bump the per-log counter, store the framed event scored by that
# id, trim the oldest beyond ``max_len`` by rank, and refresh both keys' TTL.
#   KEYS = [seq_key, log_key]
#   ARGV = [payload_json, max_len, ttl_s]
#   -> the assigned integer id (as a string)
_APPEND_LUA = """
local seq_key = KEYS[1]
local log_key = KEYS[2]
local payload = ARGV[1]
local max_len = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local id = redis.call('INCR', seq_key)
redis.call('ZADD', log_key, id, id .. '|' .. payload)
local size = redis.call('ZCARD', log_key)
if size > max_len then
    redis.call('ZREMRANGEBYRANK', log_key, 0, size - max_len - 1)
end
redis.call('PEXPIRE', seq_key, ttl)
redis.call('PEXPIRE', log_key, ttl)
return id
"""


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    """One event recovered from the log: its monotonic id + the §5.6 payload."""

    id: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """The outcome of a :meth:`EventLog.replay_after` resume request.

    ``gap`` is ``True`` when the requested ``Last-Event-ID`` is older than the
    oldest retained event — the gap is unrecoverable from the log and the client
    should fall back to a full re-seed (re-fetch the current buffer state). The
    SSE route signals this to the client with a synthetic ``resume_gap`` event so
    the UI can re-hydrate instead of silently missing events.
    """

    events: list[LoggedEvent]
    gap: bool
    last_id: int


class EventLog:
    """A capped, TTL'd, per-key append-only event log over Redis sorted sets."""

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:evlog",
        max_len: int = DEFAULT_MAX_LEN,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        # Accept a RedisClient wrapper or a raw asyncio client (mirrors the queue).
        self._redis: Any = getattr(redis, "raw", redis)
        self._ns = namespace
        self._max_len = max_len
        self._ttl_ms = max(int(ttl_s * 1000), 1000)

    # -- keys ---------------------------------------------------------------- #

    def _log_key(self, stream: str) -> str:
        return f"{self._ns}:{stream}:log"

    def _seq_key(self, stream: str) -> str:
        return f"{self._ns}:{stream}:seq"

    # -- append -------------------------------------------------------------- #

    async def append(self, stream: str, payload: dict[str, Any]) -> int | None:
        """Append ``payload`` to ``stream``'s log; return its assigned id.

        Fail-open: on any Redis error this logs and returns ``None`` so the live
        fan-out it tees off is never disturbed.
        """
        try:
            body = json.dumps(payload, separators=(",", ":"))
            raw = await self._redis.eval(
                _APPEND_LUA,
                2,
                self._seq_key(stream),
                self._log_key(stream),
                body,
                str(self._max_len),
                str(self._ttl_ms),
            )
            return int(raw)
        except Exception as exc:  # noqa: BLE001 - resume is best-effort, never fatal
            logger.warning("event_log.append_failed", stream=stream, error=str(exc))
            return None

    async def append_framed(self, stream: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append and return a *copy* of the payload with its ``id`` injected.

        Used by the tee so the same dict can be both logged and fanned out live
        with a consistent cursor the client can echo back on reconnect.
        """
        assigned = await self.append(stream, payload)
        framed = dict(payload)
        if assigned is not None:
            framed["id"] = assigned
        return framed

    # -- read / resume ------------------------------------------------------- #

    async def latest_id(self, stream: str) -> int:
        """The current high-water id (0 when the log is empty/expired)."""
        try:
            value = await self._redis.get(self._seq_key(stream))
            return int(value) if value else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_log.latest_id_failed", stream=stream, error=str(exc))
            return 0

    async def oldest_id(self, stream: str) -> int:
        """The smallest retained id (0 when the log is empty)."""
        try:
            rows = await self._redis.zrange(self._log_key(stream), 0, 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_log.oldest_id_failed", stream=stream, error=str(exc))
            return 0
        if not rows:
            return 0
        return _split_id(rows[0])

    async def replay_after(self, stream: str, last_id: int, *, limit: int = 1024) -> ResumeResult:
        """Return every logged event with id > ``last_id`` (a resume read).

        Detects an unrecoverable ``gap``: if ``last_id`` is below the oldest
        retained id and the log is non-empty, events between them have already
        been trimmed and the client must re-seed.
        """
        if last_id < 0:
            last_id = 0
        try:
            oldest = await self.oldest_id(stream)
            # gap iff we lost the boundary: the client wants events after an id we
            # no longer hold (and the log isn't simply empty).
            gap = oldest > 0 and last_id > 0 and last_id < oldest - 1
            rows = await self._redis.zrangebyscore(
                self._log_key(stream),
                f"({last_id}",
                "+inf",
                start=0,
                num=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_log.replay_failed", stream=stream, error=str(exc))
            return ResumeResult(events=[], gap=False, last_id=last_id)

        events: list[LoggedEvent] = []
        for row in rows:
            event_id, payload = _split(row)
            if event_id is None:
                continue
            events.append(LoggedEvent(id=event_id, payload=payload))
        resume_from = events[-1].id if events else last_id
        return ResumeResult(events=events, gap=gap, last_id=resume_from)

    async def size(self, stream: str) -> int:
        """How many events are retained for ``stream`` (diagnostics)."""
        try:
            return int(await self._redis.zcard(self._log_key(stream)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_log.size_failed", stream=stream, error=str(exc))
            return 0

    async def clear(self, stream: str) -> None:
        """Drop a session's log + counter (explicit session close / teardown)."""
        try:
            await self._redis.delete(self._log_key(stream), self._seq_key(stream))
        except Exception as exc:  # noqa: BLE001
            logger.warning("event_log.clear_failed", stream=stream, error=str(exc))


def _split_id(row: str) -> int:
    """Best-effort id from an ``"<id>|<json>"`` row (0 on a malformed row)."""
    head = row.split("|", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _split(row: str) -> tuple[int | None, dict[str, Any]]:
    """Decode an ``"<id>|<json>"`` row into ``(id, payload)``.

    A malformed row (truncated write, manual tampering) yields ``(None, {})`` and
    is skipped by the caller rather than crashing a resume.
    """
    head, _, body = row.partition("|")
    try:
        event_id = int(head)
    except ValueError:
        return None, {}
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, {}
    if not isinstance(payload, dict):
        return None, {}
    return event_id, payload


__all__ = [
    "DEFAULT_MAX_LEN",
    "DEFAULT_TTL_S",
    "EventLog",
    "LoggedEvent",
    "ResumeResult",
]
