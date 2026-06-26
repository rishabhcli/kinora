"""Shared, infra-free doubles for the Scheduler tests (kinora.md §4).

The Scheduler's control loop is pure given its collaborators, so these
legitimate test doubles (an in-memory shot source, a budget gate that tracks
reserves, a queue that records enqueues, a keyframe maintainer that records
ensures, and an in-memory Redis for :class:`SchedulerStore`) let the
watermark/promotion/idle/seek logic be exercised deterministically without
Redis, Postgres, or DashScope. The queue/worker tests exercise the *real* Redis
queue separately.

This module holds no tests of its own (mirrors ``tests.test_render_support``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db.models.enums import RenderPriority
from app.memory.budget_service import Reservation
from app.queue.redis_queue import EnqueueResult, EnqueueStatus

BOOK_ID = "book_demo"


@dataclass
class FakeShot:
    """A minimal ``shots`` row: just what the Scheduler reads from the span index."""

    id: str
    beat_id: str | None
    scene_id: str | None
    word_index_start: int
    # ``float | None`` (not ``float``) so FakeShot structurally satisfies the
    # SchedulerShot protocol, whose ``duration_s`` is ``float | None`` (a settable
    # protocol attribute is invariant). Lets FakeShots stand in for ShotSource.
    duration_s: float | None = 5.0
    prompt: str | None = None
    source_span: dict[str, Any] | None = None


def build_shots(
    count: int,
    *,
    spacing: int = 10,
    duration_s: float = 5.0,
    scene_id: str = "scene_001",
) -> list[FakeShot]:
    """Build ``count`` evenly-spaced shots (start = i*spacing) for the index."""
    return [
        FakeShot(
            id=f"shot_{i:04d}",
            beat_id=f"beat_{i:04d}",
            scene_id=scene_id,
            word_index_start=i * spacing,
            duration_s=duration_s,
            prompt=f"beat {i} keyframe",
        )
        for i in range(1, count + 1)
    ]


class FakeShots:
    """An in-memory §4.2 source-span index over a sorted shot list."""

    def __init__(self, shots: list[FakeShot]) -> None:
        self._shots = sorted(shots, key=lambda s: s.word_index_start)

    async def next_uncommitted_shot(self, book_id: str, after_word: int) -> FakeShot | None:
        for shot in self._shots:
            if shot.word_index_start > after_word:
                return shot
        return None

    async def resolve_word_to_shot(self, book_id: str, word_index: int) -> FakeShot | None:
        found: FakeShot | None = None
        for shot in self._shots:
            if shot.word_index_start <= word_index:
                found = shot
            else:
                break
        return found


class FakeBudget:
    """A budget gate that tracks reserves/releases (to prove the zero-video path)."""

    def __init__(
        self, *, live: bool = True, low: bool = False, remaining: float = 100_000.0
    ) -> None:
        self._live = live
        self._low = low
        self._remaining = remaining
        self.reserves: list[float] = []
        self.releases: int = 0

    def can_render_live(self) -> bool:
        return self._live

    async def is_low(self) -> bool:
        return self._low

    def is_low_at(self, remaining: float) -> bool:
        return self._low

    async def remaining(self) -> float:
        return self._remaining

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        self.reserves.append(video_seconds)
        self._remaining -= video_seconds
        return Reservation(
            id=f"res_{len(self.reserves)}",
            video_seconds=video_seconds,
            session_id=session_id,
            scene_id=scene_id,
            book_id=book_id,
        )

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        self.releases += 1
        self._remaining += reservation.video_seconds


class FakeQueue:
    """A render queue that records enqueues and dedups by idempotency key."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.cancel_token_calls: list[tuple[str, Any]] = []
        self.cancel_distant_calls: list[dict[str, Any]] = []
        self._known: dict[str, str] = {}

    async def enqueue(
        self, *, shot_hash: str, priority: RenderPriority, **kw: Any
    ) -> EnqueueResult:
        if shot_hash in self._known:
            return EnqueueResult(status=EnqueueStatus.EXISTING, job_id=self._known[shot_hash])
        job_id = str(kw.get("job_id"))
        self._known[shot_hash] = job_id
        self.enqueued.append({"shot_hash": shot_hash, "priority": priority, **kw})
        return EnqueueResult(status=EnqueueStatus.ENQUEUED, job_id=job_id)

    async def cancel_by_token(self, token: str, *, lanes: Any = None) -> int:
        self.cancel_token_calls.append((token, lanes))
        return 0

    async def cancel_distant(
        self,
        token: str,
        *,
        focus_word: int,
        velocity_wps: float,
        threshold_s: float = 120.0,
        lanes: Any = None,
    ) -> int:
        self.cancel_distant_calls.append(
            {"token": token, "focus_word": focus_word, "velocity_wps": velocity_wps,
             "threshold_s": threshold_s}
        )
        return 0

    def by_priority(self, priority: RenderPriority) -> list[dict[str, Any]]:
        return [e for e in self.enqueued if e["priority"] is priority]


class FakeKeyframes:
    """A keyframe maintainer that records ensures (and never touches budget)."""

    def __init__(self) -> None:
        self.ensured: list[dict[str, Any]] = []

    async def ensure(
        self,
        session: Any,
        *,
        book_id: str,
        beat_id: str,
        target_word: int,
        prompt: str | None = None,
    ) -> None:
        self.ensured.append({"book_id": book_id, "beat_id": beat_id, "target_word": target_word})

    @property
    def beats(self) -> list[str]:
        return [e["beat_id"] for e in self.ensured]


@dataclass
class FakeRedis:
    """An in-memory stand-in for the RedisClient JSON surface (SchedulerStore)."""

    store: dict[str, Any] = field(default_factory=dict)

    async def get_json(self, key: str) -> Any | None:
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, *, ttl_s: int | None = None) -> None:
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed


__all__ = [
    "BOOK_ID",
    "FakeBudget",
    "FakeKeyframes",
    "FakeQueue",
    "FakeRedis",
    "FakeShot",
    "FakeShots",
    "build_shots",
]
