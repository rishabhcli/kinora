"""The Scheduler's per-session control state (kinora.md §4.9).

One :class:`SchedulerSession` per active reading session holds the reading
position (focus word ``w``, velocity ``v``), the dual-watermark buffer state
(committed-seconds-ahead + the per-shot committed buffer + the hysteresis
``bursting`` flag), the trajectory cancel token, and the debounce/dwell/idle
bookkeeping. It is persisted in Redis (the hot path) and mirrored to the
durable ``sessions`` row (:class:`SessionRepo`) for recovery.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.db.models.enums import SessionMode

logger = get_logger("app.scheduler.model")

_REDIS_PREFIX = "kinora:sched:session"

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


def new_trajectory_token() -> str:
    """A fresh cancel token identifying one reading trajectory (§4.8/§12.1)."""
    return f"traj_{uuid.uuid4().hex[:16]}"


class BufferedShot(BaseModel):
    """A shot held in the committed buffer (in-flight or ready/cached)."""

    shot_id: str
    word_index_start: int
    est_duration_s: float
    #: ``inflight`` once enqueued; ``ready`` once its clip lands.
    state: Literal["inflight", "ready"] = "inflight"


class SchedulerSession(BaseModel):
    """The control-plane state for one reading session (§4.9)."""

    session_id: str
    book_id: str

    # Reading-position model (§4.3).
    focus_word: int = 0
    #: Velocity used for ETA — clamped to [0.5x, 3x] the default (§4.3).
    velocity_wps: float = 4.0
    #: Pre-clamp estimate, used only for skim detection (§4.6): a value above the
    #: clamp ceiling signals a rapid skim even though ``velocity_wps`` is capped.
    raw_velocity_wps: float = 4.0
    direction: int = 1  # +1 forward, -1 backward
    mode: SessionMode = SessionMode.VIEWER

    # Dual-watermark buffer (§4.5).
    committed_seconds_ahead: float = 0.0
    committed_buffer: list[BufferedShot] = Field(default_factory=list)
    bursting: bool = False

    # Speculative keyframe coverage (§4.4) — beats with an ensured still.
    speculative_beats: list[str] = Field(default_factory=list)

    # Budget mirror + idle bookkeeping (§4.7/§11.1).
    budget_remaining_s: float | None = None
    last_activity_ms: int | None = None

    # Cancellation / trajectory (§4.8).
    trajectory_token: str = Field(default_factory=new_trajectory_token)

    # Debounce / dwell / seek bookkeeping (§4.7/§4.8).
    last_intent_ms: int | None = None
    consecutive_forward: int = 0
    oscillating: bool = False
    fresh_samples_needed: int = 0
    pending_intent: bool = False

    # -- buffer math --------------------------------------------------------- #

    def recompute_committed_ahead(self, focus_word: int | None = None) -> float:
        """Recompute committed-seconds-ahead, pruning shots the reader passed.

        The buffer is measured in *reading-time ahead of the focus playhead*: a
        committed shot counts only while its span start is still ahead of ``w``.
        As the reader advances, consumed shots drop out — which is exactly what
        produces the §4.10 sawtooth as the buffer drains toward ``L``.
        """
        w = self.focus_word if focus_word is None else focus_word
        kept: list[BufferedShot] = []
        ahead = 0.0
        for shot in self.committed_buffer:
            if shot.word_index_start > w:
                kept.append(shot)
                ahead += shot.est_duration_s
        self.committed_buffer = kept
        self.committed_seconds_ahead = round(ahead, 6)
        return self.committed_seconds_ahead

    @property
    def inflight(self) -> dict[str, list[str]]:
        """The §4.9 in-flight view: committed shot ids + speculative beat ids."""
        return {
            "committed": [s.shot_id for s in self.committed_buffer if s.state == "inflight"],
            "speculative": list(self.speculative_beats),
        }

    def mark_ready(self, shot_id: str) -> bool:
        """Flip a buffered committed shot to ``ready`` (its clip landed)."""
        for shot in self.committed_buffer:
            if shot.shot_id == shot_id:
                shot.state = "ready"
                return True
        return False


class SchedulerStore:
    """Persist :class:`SchedulerSession` to Redis, mirroring to the durable row."""

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = _REDIS_PREFIX,
        ttl_s: int | None = 86_400,
        session_factory: SessionFactory | None = None,
    ) -> None:
        # Accepts a RedisClient wrapper (preferred — typed JSON helpers).
        self._redis = redis
        self._ns = namespace
        self._ttl = ttl_s
        self._session_factory = session_factory

    def _key(self, session_id: str) -> str:
        return f"{self._ns}:{session_id}"

    async def load(self, session_id: str) -> SchedulerSession | None:
        """Load a session's control state from Redis (``None`` if absent)."""
        data = await self._redis.get_json(self._key(session_id))
        if data is None:
            return None
        return SchedulerSession.model_validate(data)

    async def save(self, session: SchedulerSession) -> None:
        """Persist to Redis (hot path) and best-effort mirror to ``sessions``."""
        await self._redis.set_json(
            self._key(session.session_id),
            session.model_dump(mode="json"),
            ttl_s=self._ttl,
        )
        await self._mirror(session)

    async def delete(self, session_id: str) -> None:
        """Remove a session's Redis state (end of session)."""
        await self._redis.delete(self._key(session_id))

    async def _mirror(self, session: SchedulerSession) -> None:
        if self._session_factory is None:
            return
        from app.db.repositories.session import SessionRepo

        try:
            async with self._session_factory() as db:
                repo = SessionRepo(db)
                updated = await repo.update_fields(
                    session.session_id,
                    focus_word=session.focus_word,
                    velocity_wps=session.velocity_wps,
                    committed_seconds_ahead=session.committed_seconds_ahead,
                    mode=session.mode,
                    inflight=session.inflight,
                    budget_remaining_s=session.budget_remaining_s,
                    last_activity_ms=session.last_activity_ms,
                )
                if updated is None:
                    await repo.upsert(
                        session_id=session.session_id,
                        book_id=session.book_id,
                        focus_word=session.focus_word,
                        velocity_wps=session.velocity_wps,
                        committed_seconds_ahead=session.committed_seconds_ahead,
                        mode=session.mode,
                        inflight=session.inflight,
                        budget_remaining_s=session.budget_remaining_s,
                        last_activity_ms=session.last_activity_ms,
                    )
        except Exception as exc:  # durability mirror must never break the hot path
            logger.warning("scheduler.mirror_failed", session_id=session.session_id, error=str(exc))


__all__ = [
    "BufferedShot",
    "SchedulerSession",
    "SchedulerStore",
    "new_trajectory_token",
]
