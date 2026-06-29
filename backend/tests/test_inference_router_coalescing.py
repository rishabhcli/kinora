"""Tests for app.inference.router.coalescing — in-flight request dedup.

The contract: the first request for a coalesce key leads (is scheduled), later
ones follow (await the leader's result), and on settle every follower resolves
with its *own* request_id + a cache-hit flag. A request with no coalesce_key
never coalesces.
"""

from __future__ import annotations

import pytest

from app.inference.router.coalescing import CoalescingTable
from app.inference.router.protocols import InferenceResult
from app.inference.router.request import InferenceRequest


def _req(rid: str, key: str | None = None) -> InferenceRequest:
    return InferenceRequest(request_id=rid, model="m", coalesce_key=key, prompt_tokens=10)


def _result(rid: str) -> InferenceResult:
    return InferenceResult(request_id=rid, model="m", output_tokens=42, prompt_tokens=10)


def test_request_without_key_always_leads() -> None:
    table = CoalescingTable()
    a = table.admit(_req("a"))
    b = table.admit(_req("b"))
    assert a.is_leader and b.is_leader
    assert table.in_flight_keys == 0


async def test_second_same_key_request_follows_leader() -> None:
    table = CoalescingTable()
    leader = table.admit(_req("a", key="shared"))
    follower = table.admit(_req("b", key="shared"))
    assert leader.is_leader
    assert not follower.is_leader
    assert follower.follower_future is not None
    assert table.coalesced_total == 1


async def test_settle_fans_result_to_followers_with_own_id() -> None:
    table = CoalescingTable()
    table.admit(_req("leader", key="k"))
    f1 = table.admit(_req("f1", key="k"))
    f2 = table.admit(_req("f2", key="k"))
    n = table.settle("k", _result("leader"))
    assert n == 2
    assert f1.follower_future is not None and f2.follower_future is not None
    r1 = await f1.follower_future
    r2 = await f2.follower_future
    assert r1.request_id == "f1" and r2.request_id == "f2"
    assert r1.cache_hit and r2.cache_hit
    assert r1.output_tokens == 42


async def test_settle_frees_the_key() -> None:
    table = CoalescingTable()
    table.admit(_req("leader", key="k"))
    table.admit(_req("f1", key="k"))
    table.settle("k", _result("leader"))
    assert table.in_flight_keys == 0
    # A new leader can claim the freed key.
    again = table.admit(_req("leader2", key="k"))
    assert again.is_leader


async def test_fail_propagates_to_followers() -> None:
    table = CoalescingTable()
    table.admit(_req("leader", key="k"))
    f1 = table.admit(_req("f1", key="k"))
    err = RuntimeError("boom")
    n = table.fail("k", err)
    assert n == 1
    assert f1.follower_future is not None
    with pytest.raises(RuntimeError, match="boom"):
        await f1.follower_future


def test_settle_unknown_key_is_noop() -> None:
    table = CoalescingTable()
    assert table.settle("nope", _result("x")) == 0
    assert table.fail("nope", RuntimeError()) == 0


async def test_disabled_table_never_coalesces() -> None:
    table = CoalescingTable(enabled=False)
    a = table.admit(_req("a", key="shared"))
    b = table.admit(_req("b", key="shared"))
    assert a.is_leader and b.is_leader
    assert table.coalesced_total == 0


async def test_followers_of_lists_attached_requests() -> None:
    table = CoalescingTable()
    table.admit(_req("leader", key="k"))
    table.admit(_req("f1", key="k"))
    followers = table.followers_of("k")
    assert [r.request_id for r in followers] == ["f1"]


@pytest.mark.asyncio
async def test_multiple_keys_independent() -> None:
    table = CoalescingTable()
    table.admit(_req("a1", key="A"))
    fa = table.admit(_req("a2", key="A"))
    table.admit(_req("b1", key="B"))
    fb = table.admit(_req("b2", key="B"))
    assert table.in_flight_keys == 2
    table.settle("A", _result("a1"))
    assert fa.follower_future is not None and (await fa.follower_future).request_id == "a2"
    assert table.in_flight_keys == 1
    table.settle("B", _result("b1"))
    assert fb.follower_future is not None and (await fb.follower_future).request_id == "b2"
