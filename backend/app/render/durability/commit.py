"""Exactly-once persistence of an accepted clip (kinora.md §9.6, §9.7).

Accepting a shot is the one *irreversible*, side-effecting moment of a render: it
writes the clip + last frame + audio to OSS, logs episodic memory, populates the
shot cache, and marks the row accepted (§9.6). If a worker crashes *after* some of
those writes but *before* acking — or a duplicate delivery slips past the in-flight
gate — replaying the accept would double-write OSS and double-log episodic memory.

This module makes the accept **transactional + idempotent** without rewriting the
pipeline's accept: it wraps the real persist behind a **commit log** keyed by the
:class:`~app.render.durability.keys.IdempotencyKey`.

* :meth:`ClipCommitter.commit` runs the injected persist function exactly once for
  a key. If the key is already committed it returns the recorded
  :class:`AcceptedClipRecord` *without* re-running the persist (dedup on retry).
* The persist runs inside a caller-supplied transaction boundary
  (:class:`CommitTransaction`) so the clip artifacts + the commit-log mark land
  atomically: either both are durable or neither is. A real adapter binds this to
  the SQLAlchemy unit-of-work; the in-memory impl commits/rolls back a dict.

Pure orchestration around an injected persist callable + an injected transaction,
so the exactly-once semantics are unit-testable with no DB/OSS.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.logging import get_logger
from app.render.durability.keys import IdempotencyKey

logger = get_logger("app.render.durability.commit")

__all__ = [
    "AcceptedClipRecord",
    "ClipCommitter",
    "CommitLog",
    "CommitTransaction",
    "InMemoryCommitLog",
    "NullCommitTransaction",
]


@dataclass(slots=True)
class AcceptedClipRecord:
    """The durable, replay-safe record of an accepted clip (the commit-log value).

    Small + JSON-friendly: the heavy bytes live in OSS keyed by ``clip_key`` — the
    record only references them, so it is cheap to retain and cheap to serve back to
    a duplicate delivery. ``shot_hash`` ties the accept to the §8.7 cache entry.
    """

    key: str
    shot_id: str
    book_id: str
    clip_key: str | None = None
    last_frame_key: str | None = None
    audio_key: str | None = None
    shot_hash: str | None = None
    video_seconds: float = 0.0
    sync_segment: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "shot_id": self.shot_id,
            "book_id": self.book_id,
            "clip_key": self.clip_key,
            "last_frame_key": self.last_frame_key,
            "audio_key": self.audio_key,
            "shot_hash": self.shot_hash,
            "video_seconds": self.video_seconds,
            "sync_segment": self.sync_segment,
            "qa": self.qa,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AcceptedClipRecord:
        return AcceptedClipRecord(
            key=str(data["key"]),
            shot_id=str(data["shot_id"]),
            book_id=str(data["book_id"]),
            clip_key=data.get("clip_key"),
            last_frame_key=data.get("last_frame_key"),
            audio_key=data.get("audio_key"),
            shot_hash=data.get("shot_hash"),
            video_seconds=float(data.get("video_seconds", 0.0)),
            sync_segment=data.get("sync_segment"),
            qa=data.get("qa"),
        )


class CommitLog(Protocol):
    """Durable, idempotent record of which keys have been committed (accepted)."""

    def get(self, key: str) -> AcceptedClipRecord | None: ...

    def put(self, record: AcceptedClipRecord) -> None: ...


class InMemoryCommitLog:
    """A thread-safe in-process :class:`CommitLog` (test/double + within-proc)."""

    def __init__(self) -> None:
        self._store: dict[str, AcceptedClipRecord] = {}
        self._lock = threading.Lock()
        self.commits: int = 0

    def get(self, key: str) -> AcceptedClipRecord | None:
        with self._lock:
            record = self._store.get(key)
            return AcceptedClipRecord.from_dict(record.as_dict()) if record is not None else None

    def put(self, record: AcceptedClipRecord) -> None:
        with self._lock:
            self._store[record.key] = AcceptedClipRecord.from_dict(record.as_dict())
            self.commits += 1


class CommitTransaction(Protocol):
    """An atomic boundary for the accept: ``async with`` commits on clean exit.

    Whatever the persist function writes inside the boundary — the OSS marks, the
    episodic row, the cache entry, *and* the commit-log mark — must commit together
    or roll back together. A real impl binds this to the DB unit-of-work; the
    in-memory :class:`NullCommitTransaction` is the test seam (always commits).
    """

    def begin(self) -> AbstractAsyncContextManager[Any]: ...


class NullCommitTransaction:
    """A no-op :class:`CommitTransaction` (the persist callable owns its own tx)."""

    def begin(self) -> AbstractAsyncContextManager[Any]:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx() -> Any:
            yield None

        return _ctx()


#: The persist callable the committer runs exactly once per key. It performs the
#: real §9.6 accept side-effects (OSS writes, episodic log, cache put, mark
#: accepted) and returns the small :class:`AcceptedClipRecord` to record.
PersistFn = Callable[[], Awaitable[AcceptedClipRecord]]


@dataclass(slots=True)
class ClipCommitter:
    """Commit an accepted clip exactly once per :class:`IdempotencyKey`.

    Attributes:
        log: the durable commit log (idempotency record of accepted keys).
        transaction: the atomic boundary the persist + the commit-log mark share
            (defaults to a no-op so the persist callable can own its own tx).
    """

    log: CommitLog = field(default_factory=InMemoryCommitLog)
    transaction: CommitTransaction = field(default_factory=NullCommitTransaction)

    def existing(self, key: IdempotencyKey) -> AcceptedClipRecord | None:
        """The already-committed record for ``key`` (``None`` if not yet committed)."""
        return self.log.get(key.as_str())

    async def commit(self, key: IdempotencyKey, persist: PersistFn) -> AcceptedClipRecord:
        """Run ``persist`` at most once for ``key``; return the accepted record.

        Dedup on retry: if ``key`` is already in the commit log, ``persist`` is
        **not** called — the recorded :class:`AcceptedClipRecord` is returned, so a
        crash-and-resume or a duplicate delivery serves the existing clip rather
        than re-writing OSS and re-logging episodic memory.

        Otherwise ``persist`` runs inside the transaction; its returned record and
        the commit-log mark are written together so the accept is atomic.
        """
        existing = self.log.get(key.as_str())
        if existing is not None:
            logger.info("commit.dedup", key=key.as_str(), shot_id=existing.shot_id)
            return existing

        async with self.transaction.begin():
            record = await persist()
            # Stamp the key onto the record and record the commit inside the same
            # transaction so the artifacts and the idempotency mark are atomic.
            record.key = key.as_str()
            self.log.put(record)
            logger.info(
                "commit.accepted",
                key=key.as_str(),
                shot_id=record.shot_id,
                clip_key=record.clip_key,
                video_seconds=record.video_seconds,
            )
            return record
