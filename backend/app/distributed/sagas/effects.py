"""The effect ledger — exactly-once side effects under at-least-once execution.

A saga step can run **more than once**: a retry after a transient failure, or a
crash-resume that re-drives a step whose forward record was not yet durably
marked complete. That is fine for pure computation but catastrophic for side
effects — reserving budget twice, submitting two render tasks, sending two
emails. The effect ledger turns *at-least-once execution* into *exactly-once
effects* by the standard trick: every non-idempotent action is wrapped behind a
stable **idempotency key**, and the ledger records, atomically, that the key has
been applied along with the action's result. A second call with the same key
short-circuits and returns the recorded result without re-running the action.

Usage from a step handler::

    async def reserve_budget(ctx: SagaContext) -> StepResult:
        ticket = await ctx.effects.once(
            ctx.effect_key("reserve"),
            lambda: budget.reserve(seconds=5),
        )
        return StepResult.ok(reservation=ticket)

The ledger guarantees that ``budget.reserve`` runs **at most once** for that key
no matter how many times ``reserve_budget`` is replayed. The recorded result is
JSON-serialisable so the durable backends can persist it.

Two backends ship here, behind the :class:`EffectLedger` protocol:

* :class:`InMemoryEffectLedger` — an ``asyncio.Lock``-guarded dict; the reference
  used by the virtual-clock harness and most tests.
* :class:`RedisEffectLedger` — a Redis-backed ledger whose claim is a single
  ``SET NX`` (the atomic compare-and-set that makes the claim race-free across
  processes), with the result stored alongside and an optional TTL.

A Postgres-backed ledger is provided separately in :mod:`app.distributed.sagas.store`
(it shares the saga state table's transaction), keeping this module dependency-light.

The ledger also supports **compensation effects**: a forward effect can register
an *undo token* so the compensation knows exactly what to reverse (e.g. the
reservation id to release), and the compensation's own undo is itself recorded
exactly-once.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class EffectState(StrEnum):
    """Whether a recorded effect's action completed or is mid-flight."""

    PENDING = "pending"  # claimed, action started but not yet recorded as done
    APPLIED = "applied"  # action completed; result recorded


@dataclass(slots=True)
class EffectRecord:
    """A durable record that an idempotency key's effect has been claimed/applied."""

    key: str
    state: EffectState
    result: Any = None
    undo_token: Any = None
    created_at: datetime | None = None
    applied_at: datetime | None = None


@runtime_checkable
class EffectLedger(Protocol):
    """Atomic, exactly-once recording of side effects keyed by idempotency key."""

    async def get(self, key: str) -> EffectRecord | None:
        """Return the recorded effect for ``key`` (``None`` if never claimed)."""
        ...

    async def claim(self, key: str) -> bool:
        """Atomically claim ``key`` for first execution.

        Returns ``True`` for the single caller that won the claim (it must now run
        the action and then call :meth:`record`); ``False`` if the key was already
        claimed by someone else (the action must NOT be run again).
        """
        ...

    async def record(self, key: str, *, result: Any, undo_token: Any = None) -> None:
        """Record ``key`` as applied with the action's ``result`` (and optional undo)."""
        ...

    async def once(
        self,
        key: str,
        action: Callable[[], Awaitable[Any] | Any],
        *,
        undo_token: Any = None,
    ) -> Any:
        """Run ``action`` at most once for ``key``; return its (cached) result.

        The default flow: if the key is already APPLIED, return its recorded
        result; otherwise claim it, run the action, record the result, and return
        it. A concurrent loser of the claim waits for the winner's record.
        """
        ...

    async def forget(self, key: str) -> None:
        """Drop the record for ``key`` (used by tests + explicit re-arm paths)."""
        ...


def _ensure_jsonable(value: Any) -> Any:
    """Round-trip ``value`` through JSON so the durable backends can persist it.

    Raises a clear error early (at record time, in-process) rather than at the
    storage boundary, so a non-serialisable effect result is a developer error
    caught in tests.
    """
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise TypeError(
            f"effect result/undo must be JSON-serialisable, got {type(value)!r}: {exc}"
        ) from None


class _OnceMixin:
    """Shared :meth:`once` implementation in terms of claim/get/record.

    Concrete ledgers provide the atomic primitives; this mixin composes them into
    the run-at-most-once flow, including the loser-waits-for-winner case so two
    concurrent callers of the same key both observe the single result.
    """

    # These are provided by the concrete ledger (overridden below).
    async def get(self, key: str) -> EffectRecord | None:  # noqa: D102
        raise NotImplementedError

    async def claim(self, key: str) -> bool:  # noqa: D102
        raise NotImplementedError

    async def record(self, key: str, *, result: Any, undo_token: Any = None) -> None:  # noqa: D102
        raise NotImplementedError

    async def forget(self, key: str) -> None:  # noqa: D102
        raise NotImplementedError

    async def once(
        self,
        key: str,
        action: Callable[[], Awaitable[Any] | Any],
        *,
        undo_token: Any = None,
    ) -> Any:
        existing = await self.get(key)
        if existing is not None and existing.state is EffectState.APPLIED:
            return existing.result
        won = await self.claim(key)
        if not won:
            # Someone else is/was running it. Poll briefly for their record; this
            # is bounded because the winner records under the same lock/CAS.
            for _ in range(1000):
                rec = await self.get(key)
                if rec is not None and rec.state is EffectState.APPLIED:
                    return rec.result
                await asyncio.sleep(0)
            # Winner crashed mid-flight without recording: the key is PENDING. We
            # cannot safely re-run (the action may have partially applied), so we
            # surface it — the engine treats this as a retryable step failure and
            # the orphaned PENDING claim is reclaimed by the reaper.
            raise EffectClaimStalled(key)
        try:
            result = await _maybe_await(action)
        except Exception:
            # The action raised *before* it could be recorded, so its effect did
            # not complete — release the claim so a retry of this step (same
            # process) can re-claim and re-run it cleanly. A genuine cross-process
            # crash (the process dies here) instead leaves the PENDING claim for
            # the reaper, which is the conservative "don't re-run" path above.
            await self.forget(key)
            raise
        await self.record(key, result=result, undo_token=undo_token)
        return result


class EffectClaimStalled(RuntimeError):  # noqa: N818 - public name in the effect contract
    """Raised when an effect key is claimed (PENDING) but never recorded applied.

    Indicates the original executor crashed between claiming and recording. The
    engine treats it as a retryable failure; the saga reaper clears stale PENDING
    claims so a later attempt can re-claim cleanly.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"effect {key!r} is claimed but not applied (stalled executor)")
        self.key = key


async def _maybe_await(action: Callable[[], Awaitable[Any] | Any]) -> Any:
    result = action()
    if asyncio.iscoroutine(result):
        return await result
    return result


class InMemoryEffectLedger(_OnceMixin):
    """An in-process, lock-guarded reference :class:`EffectLedger`.

    Not durable across processes — but it makes the exactly-once semantics easy to
    assert in the virtual-clock harness, and it is the ledger most tests use.
    """

    def __init__(self, *, clock: Any = None) -> None:
        self._clock = clock
        self._records: dict[str, EffectRecord] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> datetime | None:
        return self._clock.now() if self._clock is not None else None

    async def get(self, key: str) -> EffectRecord | None:
        async with self._lock:
            rec = self._records.get(key)
            return _copy(rec) if rec is not None else None

    async def claim(self, key: str) -> bool:
        async with self._lock:
            if key in self._records:
                return False
            self._records[key] = EffectRecord(
                key=key, state=EffectState.PENDING, created_at=self._now()
            )
            return True

    async def record(self, key: str, *, result: Any, undo_token: Any = None) -> None:
        result = _ensure_jsonable(result)
        undo_token = _ensure_jsonable(undo_token)
        async with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = EffectRecord(key=key, state=EffectState.PENDING, created_at=self._now())
                self._records[key] = rec
            rec.state = EffectState.APPLIED
            rec.result = result
            rec.undo_token = undo_token
            rec.applied_at = self._now()

    async def forget(self, key: str) -> None:
        async with self._lock:
            self._records.pop(key, None)

    async def reap_stalled(self) -> int:
        """Drop PENDING (never-applied) claims so they can be re-claimed.

        In a single process a PENDING claim only lingers if the claimer raised
        between :meth:`claim` and :meth:`record`; clearing it lets the next attempt
        re-run the action. Returns how many stale claims were cleared.
        """
        async with self._lock:
            stale = [k for k, r in self._records.items() if r.state is EffectState.PENDING]
            for k in stale:
                del self._records[k]
            return len(stale)

    @property
    def applied_keys(self) -> set[str]:
        """Keys whose effect has been recorded as applied (test introspection)."""
        return {k for k, r in self._records.items() if r.state is EffectState.APPLIED}


def _copy(rec: EffectRecord) -> EffectRecord:
    return EffectRecord(
        key=rec.key,
        state=rec.state,
        result=rec.result,
        undo_token=rec.undo_token,
        created_at=rec.created_at,
        applied_at=rec.applied_at,
    )


# Claim iff free: SET NX on the pending marker. KEYS=[claim_key]; ARGV=[token, ttl_ms]
_CLAIM_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return 1
else
    return 0
end
"""


@dataclass(slots=True)
class _RedisEnvelope:
    """The JSON envelope stored at the effect's result key in Redis."""

    state: str
    result: Any = None
    undo_token: Any = None
    created_at: str | None = None
    applied_at: str | None = None

    def dumps(self) -> str:
        return json.dumps(
            {
                "state": self.state,
                "result": self.result,
                "undo_token": self.undo_token,
                "created_at": self.created_at,
                "applied_at": self.applied_at,
            }
        )


class RedisEffectLedger(_OnceMixin):
    """A Redis-backed, cross-process :class:`EffectLedger`.

    The claim is a single ``SET NX PX`` on a per-key marker (atomic across the
    fleet); the applied result lives in a companion key as a JSON envelope. The
    optional ``ttl_ms`` bounds how long records linger — set it comfortably longer
    than the longest saga so a legitimate replay still finds the record, but short
    enough that the keyspace doesn't grow without bound.
    """

    def __init__(
        self,
        redis: Any,
        *,
        namespace: str = "kinora:saga:effect",
        ttl_ms: int = 7 * 24 * 60 * 60 * 1000,
        clock: Any = None,
    ) -> None:
        self._redis = getattr(redis, "raw", redis)
        self._ns = namespace
        self._ttl_ms = ttl_ms
        self._clock = clock

    def _claim_key(self, key: str) -> str:
        return f"{self._ns}:claim:{key}"

    def _result_key(self, key: str) -> str:
        return f"{self._ns}:result:{key}"

    def _now_iso(self) -> str | None:
        return self._clock.now().isoformat() if self._clock is not None else None

    async def get(self, key: str) -> EffectRecord | None:
        raw = await self._redis.get(self._result_key(key))
        if raw is not None:
            env = json.loads(raw)
            return EffectRecord(
                key=key,
                state=EffectState(env["state"]),
                result=env.get("result"),
                undo_token=env.get("undo_token"),
            )
        # No applied result; is there an outstanding claim?
        claimed = await self._redis.get(self._claim_key(key))
        if claimed is not None:
            return EffectRecord(key=key, state=EffectState.PENDING)
        return None

    async def claim(self, key: str) -> bool:
        won = await self._redis.eval(
            _CLAIM_LUA, 1, self._claim_key(key), "1", str(self._ttl_ms)
        )
        return bool(int(won))

    async def record(self, key: str, *, result: Any, undo_token: Any = None) -> None:
        env = _RedisEnvelope(
            state=EffectState.APPLIED.value,
            result=_ensure_jsonable(result),
            undo_token=_ensure_jsonable(undo_token),
            applied_at=self._now_iso(),
        )
        await self._redis.set(self._result_key(key), env.dumps(), px=self._ttl_ms)

    async def forget(self, key: str) -> None:
        await self._redis.delete(self._claim_key(key), self._result_key(key))


__all__ = [
    "EffectClaimStalled",
    "EffectLedger",
    "EffectRecord",
    "EffectState",
    "InMemoryEffectLedger",
    "RedisEffectLedger",
]
