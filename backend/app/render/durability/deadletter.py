"""The render-engine dead-letter sink for poison shots (kinora.md §4.11, §12.1).

Distinct from :class:`app.queue.dlq.DeadLetterQueue` (the *operability* tool over
the queue's job-id DLQ list — peek / replay / purge). This is the **render-side**
sink the :class:`~app.render.durability.guard.DurableRenderGuard` routes a shot to
once the poison tracker quarantines it: the per-shot fact "this render keeps
crashing, a human should look".

A dead-letter here records the shot (id, book, idempotency key, error, failure
count) so it is queryable for triage and logs a ``poison`` defect — while the
render path itself still ships the guaranteed bottom-rung audio-text card, so a
single pathological shot never hard-stops the film (§4.11).

The sink is a Protocol with an in-memory impl for tests; a production adapter logs
the defect through the ``DefectRepo`` and (optionally) mirrors the entry onto the
queue DLQ list so the existing replay tooling can re-drive it once triaged.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger("app.render.durability.deadletter")

__all__ = [
    "DeadLetterEntry",
    "DeadLetterSink",
    "InMemoryDeadLetterSink",
    "NullDeadLetterSink",
    "RepoDeadLetterSink",
]


@dataclass(frozen=True, slots=True)
class DeadLetterEntry:
    """A poison shot routed for human triage."""

    shot_id: str
    book_id: str
    key: str
    error: str
    failures: int
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "book_id": self.book_id,
            "key": self.key,
            "error": self.error,
            "failures": self.failures,
            "at": self.at,
        }


class DeadLetterSink(Protocol):
    """Route a quarantined shot to a triage sink (must never raise on the hot path)."""

    async def dead_letter(
        self, *, shot_id: str, book_id: str, key: str, error: str, failures: int
    ) -> None: ...


class NullDeadLetterSink:
    """A no-op sink (the guard default; just logs)."""

    async def dead_letter(
        self, *, shot_id: str, book_id: str, key: str, error: str, failures: int
    ) -> None:
        logger.error(
            "deadletter.shot",
            shot_id=shot_id,
            book_id=book_id,
            key=key,
            error=error,
            failures=failures,
        )


class InMemoryDeadLetterSink:
    """A thread-safe in-process sink that retains entries (test/double + inspection)."""

    def __init__(self) -> None:
        self._entries: list[DeadLetterEntry] = []
        self._lock = threading.Lock()

    async def dead_letter(
        self, *, shot_id: str, book_id: str, key: str, error: str, failures: int
    ) -> None:
        entry = DeadLetterEntry(
            shot_id=shot_id, book_id=book_id, key=key, error=error, failures=failures
        )
        with self._lock:
            self._entries.append(entry)
        logger.error("deadletter.shot", **entry.as_dict())

    def entries(self) -> list[DeadLetterEntry]:
        """A snapshot of the dead-lettered shots (newest last)."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: A callable that logs a ``poison`` defect (matches ``DefectRepo.log``'s shape).
DefectLogger = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class RepoDeadLetterSink:
    """A production sink that logs a ``poison`` defect through an injected logger.

    Wraps the defect write so a triage row is visible in the defects feed. Guarded:
    a logging failure is swallowed (the shot already ships its bottom-rung card; a
    failed *defect write* must never crash the render). ``also`` optionally chains a
    second sink (e.g. mirror onto the queue DLQ list for the replay tooling).
    """

    log_defect: DefectLogger
    also: DeadLetterSink | None = None

    async def dead_letter(
        self, *, shot_id: str, book_id: str, key: str, error: str, failures: int
    ) -> None:
        try:
            await self.log_defect(
                book_id=book_id,
                kind="poison",
                shot_id=shot_id,
                detail={"error": error, "failures": failures, "key": key},
            )
        except Exception as exc:  # noqa: BLE001 - a defect-log failure must not crash
            logger.warning("deadletter.defect_log_failed", shot_id=shot_id, error=str(exc))
        if self.also is not None:
            await self.also.dead_letter(
                shot_id=shot_id, book_id=book_id, key=key, error=error, failures=failures
            )
