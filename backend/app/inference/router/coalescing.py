"""Request coalescing — two identical in-flight requests pay for one (§12.3).

When two sessions ask for the *same* generation at the same time (the §12.3
"request-level dedup" row — the §8.7 shot-hash idea generalised to any inference
call), the router executes it **once** and fans the single result out to every
waiter. This both halves the spend and removes a class of duplicated load the
fair-share scheduler would otherwise have to arbitrate.

The :class:`CoalescingTable` is the bookkeeping for that:

* the **first** request for a ``coalesce_key`` becomes the *leader* and is the
  one actually scheduled;
* later requests for a key with a live leader become *followers* — they are not
  enqueued, and their futures resolve from the leader's result;
* when the leader completes (or fails), every follower is settled with the same
  outcome and the key is freed.

It is transport-agnostic and async-future based (``asyncio.Future``), so the
router awaits a follower's future exactly as it would await a normal dispatch.
Determinism: no clock, no I/O — only future plumbing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .protocols import InferenceResult
from .request import InferenceRequest


@dataclass
class _Group:
    """All requests sharing one coalesce key while a leader is in flight."""

    leader: InferenceRequest
    followers: list[InferenceRequest] = field(default_factory=list)
    waiters: list[asyncio.Future[InferenceResult]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return 1 + len(self.followers)


@dataclass(slots=True)
class CoalesceOutcome:
    """What :meth:`CoalescingTable.admit` decided for one request.

    Exactly one of ``leader``/``follower_future`` is meaningful:

    * ``is_leader`` → schedule this request normally; on completion call
      :meth:`CoalescingTable.settle`.
    * else → await ``follower_future``; do **not** schedule it.
    """

    is_leader: bool
    coalesce_key: str
    follower_future: asyncio.Future[InferenceResult] | None = None


class CoalescingTable:
    """Tracks in-flight coalesce groups and fans results out to followers."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._groups: dict[str, _Group] = {}
        self._coalesced_total = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def in_flight_keys(self) -> int:
        return len(self._groups)

    @property
    def coalesced_total(self) -> int:
        """Cumulative count of follower requests served off a leader."""
        return self._coalesced_total

    def admit(self, request: InferenceRequest) -> CoalesceOutcome:
        """Register ``request``; decide whether it leads or follows.

        Coalescing is **opt-in per request**: a request whose ``coalesce_key`` is
        ``None`` (the default → its own ``request_id``) can never collide with
        another, so it always leads. Disabling the table makes every request a
        leader.
        """
        key = request.effective_coalesce_key
        if not self._enabled or request.coalesce_key is None:
            return CoalesceOutcome(is_leader=True, coalesce_key=key)

        group = self._groups.get(key)
        if group is None:
            self._groups[key] = _Group(leader=request)
            return CoalesceOutcome(is_leader=True, coalesce_key=key)

        # A leader is already in flight for this key → follow it.
        future: asyncio.Future[InferenceResult] = asyncio.get_running_loop().create_future()
        group.followers.append(request)
        group.waiters.append(future)
        self._coalesced_total += 1
        return CoalesceOutcome(is_leader=False, coalesce_key=key, follower_future=future)

    def followers_of(self, coalesce_key: str) -> list[InferenceRequest]:
        """Follower requests currently attached to a leader (for metrics)."""
        group = self._groups.get(coalesce_key)
        return list(group.followers) if group else []

    def settle(self, coalesce_key: str, result: InferenceResult) -> int:
        """Resolve every follower of ``coalesce_key`` with the leader's result.

        The result is rewritten per-follower so each waiter sees *its own*
        ``request_id`` (the cache-hit flag is set, since a follower spent no
        compute of its own). Returns the number of followers settled. Idempotent:
        a key with no group settles nothing.
        """
        group = self._groups.pop(coalesce_key, None)
        if group is None:
            return 0
        for follower, future in zip(group.followers, group.waiters, strict=True):
            if not future.done():
                future.set_result(
                    InferenceResult(
                        request_id=follower.request_id,
                        model=result.model,
                        output_tokens=result.output_tokens,
                        prompt_tokens=result.prompt_tokens,
                        cache_hit=True,
                        accepted_tokens=result.accepted_tokens,
                        error=result.error,
                    )
                )
        return len(group.followers)

    def fail(self, coalesce_key: str, error: BaseException) -> int:
        """Fail every follower of ``coalesce_key`` with ``error``; free the key."""
        group = self._groups.pop(coalesce_key, None)
        if group is None:
            return 0
        for future in group.waiters:
            if not future.done():
                future.set_exception(error)
        return len(group.waiters)


__all__ = ["CoalesceOutcome", "CoalescingTable"]
