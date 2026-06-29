"""Redis-backed effect ledger + lock manager, exercised over a tiny in-process fake.

The package's :class:`~app.distributed.sagas.effects.RedisEffectLedger` and
:class:`~app.distributed.sagas.locks.RedisLockManager` talk to Redis through a
minimal surface: ``GET`` / ``SET`` (with ``NX`` / ``PX``) / ``DELETE`` plus three
small ``EVAL`` Lua scripts (claim, lock-acquire, lock-renew/release). The project's
shared :class:`app.queue.fakeredis.FakeAsyncRedis` only interprets the render
queue's two Lua scripts, so this module ships a tiny purpose-built fake that
interprets *exactly* the scripts these backends use — letting us prove the
cross-process primitives with zero infrastructure (no real Redis, no credits).

If the backends' Lua changes, this fake's ``eval`` must be updated in lockstep —
the same contract the queue's fake documents for itself.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.distributed.sagas.effects import EffectState, RedisEffectLedger
from app.distributed.sagas.locks import FencedResource, RedisLockManager, StaleFenceError
from app.jobs.clock import ManualClock


class _MiniRedis:
    """A tiny in-process Redis interpreting just the saga backends' surface."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self._expiry: dict[str, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _expire_check(self, key: str) -> None:
        exp = self._expiry.get(key)
        if exp is not None and self._now() >= exp:
            self._kv.pop(key, None)
            self._expiry.pop(key, None)

    async def get(self, key: str) -> str | None:
        self._expire_check(key)
        return self._kv.get(key)

    async def set(
        self, key: str, value: Any, *, nx: bool = False, px: int | None = None
    ) -> bool | None:
        self._expire_check(key)
        if nx and key in self._kv:
            return None
        self._kv[key] = str(value)
        if px is not None:
            self._expiry[key] = self._now() + px / 1000.0
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if self._kv.pop(key, None) is not None:
                n += 1
            self._expiry.pop(key, None)
        return n

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        keys = [str(a) for a in args[:numkeys]]
        argv = [str(a) for a in args[numkeys:]]
        s = script.strip()
        # Effect-claim / lock-acquire-without-fence: SET NX PX, return 1/0.
        if "'NX', 'PX'" in s and "INCR" not in s and "PEXPIRE" not in s:
            ok = await self.set(keys[0], argv[0], nx=True, px=int(argv[1]))
            return 1 if ok else 0
        # Lock acquire with fence: SET NX PX then INCR fence (or -1).
        if "'NX', 'PX'" in s and "INCR" in s:
            ok = await self.set(keys[0], argv[0], nx=True, px=int(argv[1]))
            if not ok:
                return -1
            self._expire_check(keys[1])
            fence = int(self._kv.get(keys[1], "0")) + 1
            self._kv[keys[1]] = str(fence)
            return fence
        # Lock renew (owner-scoped PEXPIRE).
        if "PEXPIRE" in s:
            if (await self.get(keys[0])) == argv[0]:
                self._expiry[keys[0]] = self._now() + int(argv[1]) / 1000.0
                return 1
            return 0
        # Owner-scoped DEL (release).
        if "DEL" in s:
            if (await self.get(keys[0])) == argv[0]:
                return await self.delete(keys[0])
            return 0
        raise AssertionError(f"_MiniRedis.eval: unrecognised script:\n{s}")


# --------------------------------------------------------------------------- #
# RedisEffectLedger
# --------------------------------------------------------------------------- #
async def test_redis_effect_ledger_exactly_once() -> None:
    ledger = RedisEffectLedger(_MiniRedis(), clock=ManualClock())
    calls = {"n": 0}

    async def action() -> dict[str, int]:
        calls["n"] += 1
        return {"v": 1}

    a = await ledger.once("k1", action)
    b = await ledger.once("k1", action)
    assert a == b == {"v": 1}
    assert calls["n"] == 1
    rec = await ledger.get("k1")
    assert rec is not None and rec.state is EffectState.APPLIED


async def test_redis_effect_ledger_claim_then_forget() -> None:
    ledger = RedisEffectLedger(_MiniRedis())
    assert await ledger.claim("k") is True
    assert await ledger.claim("k") is False
    await ledger.record("k", result="done", undo_token={"id": 5})
    rec = await ledger.get("k")
    assert rec is not None and rec.result == "done" and rec.undo_token == {"id": 5}
    await ledger.forget("k")
    assert await ledger.get("k") is None


async def test_redis_effect_release_on_exception_allows_retry() -> None:
    ledger = RedisEffectLedger(_MiniRedis())
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    with pytest.raises(RuntimeError):
        await ledger.once("k", flaky)
    assert await ledger.get("k") is None  # claim released
    assert await ledger.once("k", flaky) == "ok"
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# RedisLockManager (+ fencing)
# --------------------------------------------------------------------------- #
async def test_redis_lock_mutual_exclusion_and_fencing() -> None:
    clock = ManualClock()
    mgr = RedisLockManager(_MiniRedis(), clock=clock)
    a = await mgr.acquire("canon:b1", ttl_s=30)
    assert a is not None
    # Held → a second contender fails.
    assert await mgr.acquire("canon:b1", ttl_s=30) is None
    assert await mgr.release(a) is True
    b = await mgr.acquire("canon:b1", ttl_s=30)
    assert b is not None
    assert b.fence > a.fence  # monotonic fencing token across reacquire

    # Fencing enforcement: the released holder's stale token is rejected.
    resource = FencedResource("canon:b1")
    await resource.guard(b.fence)
    with pytest.raises(StaleFenceError):
        await resource.guard(a.fence)


async def test_redis_lock_renew_owner_scoped() -> None:
    mgr = RedisLockManager(_MiniRedis(), clock=ManualClock())
    a = await mgr.acquire("r", owner="A", ttl_s=30)
    assert a is not None
    renewed = await mgr.renew(a, ttl_s=30)
    assert renewed is not None and renewed.owner == "A"
    # A foreign lease cannot renew or release.
    foreign = type(a)(resource="r", owner="B", fence=a.fence, expires_at=a.expires_at)
    assert await mgr.renew(foreign, ttl_s=30) is None
    assert await mgr.release(foreign) is False
    # The true owner can release.
    assert await mgr.release(a) is True
