"""The §7.2 conflict log — a per-session, refresh-survivable record of surfaced
disputes and their resolutions.

Kept in Redis (one small JSON map per session, ``conflict_id -> record``) so both
sides of the negotiation can append: the **render worker** when it surfaces a
conflict, and the **director route** when the reader resolves one. The
``GET /sessions/{id}/conflicts`` endpoint replays it so a refreshed client can
reload the Crew-dispute state instead of losing it with the socket.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.queue.redis_queue import conflict_history_key

#: Conflicts live a day — long enough to survive a refresh, short enough to expire.
_TTL_S = 86_400

#: The fields a stored record (or a raw conflict object) projects onto.
_BASE_FIELDS = ("shot_id", "claim", "canon_fact", "raised_by", "current_beat")


class RedisJson(Protocol):
    """The slice of the Redis client the conflict log needs."""

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None: ...


def project_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a stored record (or a raw conflict object) onto the history fields."""
    return {
        "conflict_id": str(record.get("conflict_id", "")),
        "shot_id": record.get("shot_id"),
        "claim": record.get("claim"),
        "canon_fact": record.get("canon_fact"),
        "raised_by": record.get("raised_by"),
        "current_beat": record.get("current_beat"),
        "options": record.get("options") or [],
        "resolved": bool(record.get("resolved", False)),
        "chosen_option": record.get("chosen_option"),
        "reasoning": record.get("reasoning"),
    }


async def record_conflict_history(
    redis: RedisJson,
    session_id: str,
    *,
    conflict: dict[str, Any] | None,
    conflict_id: str,
    option: str | None = None,
    reasoning: str | None = None,
) -> None:
    """Merge a conflict into the session's §7.2 log (read-modify-write).

    Called with ``option=None`` when a conflict is *surfaced* (resolved stays
    False) and with an ``option`` when the Director *resolves* it.
    """
    key = conflict_history_key(session_id)
    raw = await redis.get_json(key)
    hist: dict[str, Any] = raw if isinstance(raw, dict) else {}
    prev = hist[conflict_id] if isinstance(hist.get(conflict_id), dict) else {}
    base = conflict or {}

    record: dict[str, Any] = {**prev, "conflict_id": conflict_id}
    for field in _BASE_FIELDS:
        record[field] = base.get(field) if base.get(field) is not None else prev.get(field)
    record["options"] = base.get("options") or prev.get("options") or []
    if option is not None:
        record["resolved"] = True
        record["chosen_option"] = option
        record["reasoning"] = reasoning
    else:
        record.setdefault("resolved", False)

    hist[conflict_id] = record
    await redis.set_json(key, hist, ttl_s=_TTL_S)


async def load_conflict_history(redis: RedisJson, session_id: str) -> list[dict[str, Any]]:
    """Replay the session's §7.2 conflict log (surfaced + resolved), newest last."""
    raw = await redis.get_json(conflict_history_key(session_id))
    hist = raw if isinstance(raw, dict) else {}
    return [project_record(r) for r in hist.values() if isinstance(r, dict)]


__all__ = ["load_conflict_history", "project_record", "record_conflict_history"]
