"""Exactly-once admission control for render deliveries (kinora.md §9.7, §12.1).

The Redis queue is at-least-once: a worker crash between *running* a render and
*acking* it leaves the job to be re-claimed, and a queue replay or a duplicate
enqueue can hand the same shot to two workers. Without a guard, that re-delivery
re-runs the render — re-spending Wan video-seconds and re-writing OSS.

This module is the **admission gate** keyed by the content identity
:class:`~app.render.durability.keys.IdempotencyKey` (``shot_id`` + spec digest):

* :meth:`IdempotencyGuard.begin` atomically claims the key. The first caller wins
  a :class:`Lease` and proceeds; a second caller for an *in-flight* key is told to
  back off (``IN_FLIGHT``); a caller for an *already completed* key is told the
  recorded result so it can short-circuit (``COMPLETED``) — never re-render.
* :meth:`IdempotencyGuard.complete` records the terminal result against the key so
  every later delivery resolves to ``COMPLETED``.
* :meth:`IdempotencyGuard.fail` releases the in-flight claim so a *transient*
  failure can be retried by the next delivery (a permanent failure leaves the
  claim recorded as completed-degraded by the caller).

A claim carries a ``fence`` token + an expiry so a crashed holder's stale claim is
reclaimable (mirrors the queue lease): :meth:`begin` steals an *expired* in-flight
claim rather than wedging the shot forever. The store is a Protocol with an
in-memory impl for tests; a Redis ``SET NX PX`` adapter drops in behind it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.logging import get_logger
from app.observability import metrics
from app.render.durability.keys import IdempotencyKey

logger = get_logger("app.render.durability.idempotency")

__all__ = [
    "Admission",
    "ClaimRecord",
    "ClaimState",
    "IdempotencyGuard",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "Lease",
]


class ClaimState(StrEnum):
    """Lifecycle of a render claim under the idempotency key."""

    #: Some worker holds an unexpired claim and is rendering this key right now.
    IN_FLIGHT = "in_flight"
    #: The render finished terminally; ``result`` holds the recorded outcome.
    COMPLETED = "completed"


@dataclass(slots=True)
class ClaimRecord:
    """The stored state of one idempotency key (in-flight lease or final result)."""

    key: str
    state: ClaimState
    fence: int = 0
    #: Wall-clock epoch seconds when an IN_FLIGHT claim may be stolen (0 = n/a).
    expires_at: float = 0.0
    #: The recorded terminal result for a COMPLETED key (small + JSON-friendly).
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state.value,
            "fence": self.fence,
            "expires_at": self.expires_at,
            "result": self.result,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ClaimRecord:
        return ClaimRecord(
            key=str(data["key"]),
            state=ClaimState(str(data["state"])),
            fence=int(data.get("fence", 0)),
            expires_at=float(data.get("expires_at", 0.0)),
            result=data.get("result"),
        )


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof that this worker holds the in-flight claim for a key.

    The ``fence`` is a monotone token: ``complete``/``fail`` only take effect if the
    stored claim still carries this fence, so a worker whose claim was stolen after
    a stall can never clobber the new holder's result (fencing, §12.1).
    """

    key: IdempotencyKey
    fence: int


class Admission(StrEnum):
    """The verdict of trying to admit a delivery for a key."""

    #: This caller won the claim — proceed to render (carries a :class:`Lease`).
    PROCEED = "proceed"
    #: Another live worker is rendering this key — this delivery should defer.
    IN_FLIGHT = "in_flight"
    #: This key already finished — short-circuit to the recorded result.
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """The outcome of :meth:`IdempotencyGuard.begin`."""

    admission: Admission
    lease: Lease | None = None
    result: dict[str, Any] | None = None


class IdempotencyStore(Protocol):
    """Atomic compare-and-set persistence of :class:`ClaimRecord`s.

    The whole exactly-once guarantee rests on :meth:`try_claim` being atomic — in
    production a Lua/`SET NX` Redis op; here an in-process lock. ``put`` records the
    terminal/release transition (fenced by the caller).
    """

    def get(self, key: str) -> ClaimRecord | None: ...

    def try_claim(self, key: str, *, ttl_s: float, now: float) -> ClaimRecord | None:
        """Atomically claim ``key`` if absent or expired; return the held record.

        Returns the (new or stolen) IN_FLIGHT record this caller now holds, or
        ``None`` when a live (unexpired) claim or a COMPLETED record blocks it.
        """
        ...

    def put(self, record: ClaimRecord) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryIdempotencyStore:
    """A thread-safe in-process :class:`IdempotencyStore` (test/double + within-proc)."""

    def __init__(self) -> None:
        self._store: dict[str, ClaimRecord] = {}
        self._lock = threading.Lock()
        self._fence = 0

    def get(self, key: str) -> ClaimRecord | None:
        with self._lock:
            record = self._store.get(key)
            return ClaimRecord.from_dict(record.as_dict()) if record is not None else None

    def try_claim(self, key: str, *, ttl_s: float, now: float) -> ClaimRecord | None:
        with self._lock:
            existing = self._store.get(key)
            if existing is not None:
                if existing.state is ClaimState.COMPLETED:
                    return None
                if existing.expires_at > now:
                    return None  # a live in-flight claim blocks us
                logger.info("idempotency.steal_expired", key=key, prev_fence=existing.fence)
            self._fence += 1
            record = ClaimRecord(
                key=key,
                state=ClaimState.IN_FLIGHT,
                fence=self._fence,
                expires_at=now + ttl_s,
            )
            self._store[key] = record
            return ClaimRecord.from_dict(record.as_dict())

    def put(self, record: ClaimRecord) -> None:
        with self._lock:
            self._store[record.key] = ClaimRecord.from_dict(record.as_dict())

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


@dataclass(slots=True)
class IdempotencyGuard:
    """Admit at-most-one live render per :class:`IdempotencyKey` (exactly-once).

    Attributes:
        store: where claims live (durable in production behind the Protocol).
        lease_ttl_s: how long a claim is considered live before it can be stolen
            (set well above a worst-case render so a slow render is never stolen;
            the worker still heartbeats its *queue* lease independently).
    """

    store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    lease_ttl_s: float = 1800.0

    def begin(self, key: IdempotencyKey, *, now: float | None = None) -> AdmissionResult:
        """Try to admit a delivery for ``key`` (the exactly-once gate).

        * ``PROCEED`` — this caller won the claim; render and then call
          :meth:`complete` / :meth:`fail`.
        * ``COMPLETED`` — the key already finished; the recorded ``result`` is
          returned so the caller short-circuits without re-rendering.
        * ``IN_FLIGHT`` — a live worker holds the claim; the caller defers.
        """
        now = time.time() if now is None else now
        held = self.store.try_claim(key.as_str(), ttl_s=self.lease_ttl_s, now=now)
        if held is not None:
            logger.info("idempotency.admit", key=key.as_str(), fence=held.fence)
            return AdmissionResult(
                admission=Admission.PROCEED, lease=Lease(key=key, fence=held.fence)
            )
        # Blocked: distinguish a finished key from a live in-flight one.
        record = self.store.get(key.as_str())
        if record is not None and record.state is ClaimState.COMPLETED:
            metrics.inc_render_dedup("completed")
            logger.info("idempotency.dedup_completed", key=key.as_str())
            return AdmissionResult(admission=Admission.COMPLETED, result=record.result)
        metrics.inc_render_dedup("in_flight")
        logger.info("idempotency.dedup_in_flight", key=key.as_str())
        return AdmissionResult(admission=Admission.IN_FLIGHT)

    def complete(self, lease: Lease, result: dict[str, Any] | None = None) -> bool:
        """Record ``key`` as terminally completed (fenced). Returns False if stolen.

        A later delivery for the same key then resolves to ``COMPLETED`` and serves
        ``result`` rather than re-rendering — the exactly-once persistence guarantee.
        """
        key = lease.key.as_str()
        current = self.store.get(key)
        if current is not None and current.fence != lease.fence:
            logger.warning(
                "idempotency.complete_fenced_out",
                key=key,
                held=lease.fence,
                current=current.fence,
            )
            return False
        self.store.put(
            ClaimRecord(
                key=key,
                state=ClaimState.COMPLETED,
                fence=lease.fence,
                result=result,
            )
        )
        logger.info("idempotency.complete", key=key, fence=lease.fence)
        return True

    def fail(self, lease: Lease) -> bool:
        """Release a transient-failed claim so the next delivery may retry (fenced).

        Returns False if the claim was already stolen (do nothing — the new holder
        owns it now). A permanent failure should call :meth:`complete` instead so
        the shot is never retried into a crash-loop.
        """
        key = lease.key.as_str()
        current = self.store.get(key)
        if current is None:
            return True
        if current.fence != lease.fence:
            return False
        if current.state is ClaimState.COMPLETED:
            return True  # someone already finalised it; leave the result intact
        self.store.delete(key)
        logger.info("idempotency.release", key=key, fence=lease.fence)
        return True
