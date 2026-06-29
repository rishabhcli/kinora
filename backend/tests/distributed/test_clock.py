"""Tests for the replication hybrid-logical clock (clock.py)."""

from __future__ import annotations

import itertools

import pytest

from app.distributed.replication.clock import (
    DEFAULT_MAX_SKEW_MS,
    HybridLogicalClock,
    HybridTimestamp,
    ManualClock,
    NodeId,
)


def test_nodeid_orders_by_region_then_node() -> None:
    assert NodeId("ap", "a") < NodeId("ap", "b")
    assert NodeId("ap", "z") < NodeId("eu", "a")


def test_nodeid_roundtrips_via_string() -> None:
    n = NodeId("us-west", "n3")
    assert NodeId.parse(str(n)) == n
    assert str(n) == "us-west/n3"


def test_nodeid_parse_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        NodeId.parse("noseparator")


def test_timestamp_total_order_is_lexicographic() -> None:
    n1 = NodeId("a", "1")
    n2 = NodeId("a", "2")
    assert HybridTimestamp(10, 0, n1) < HybridTimestamp(10, 1, n1)
    assert HybridTimestamp(10, 5, n1) < HybridTimestamp(11, 0, n1)
    # node breaks an otherwise-identical tie.
    assert HybridTimestamp(10, 0, n1) < HybridTimestamp(10, 0, n2)


def test_timestamp_encoding_roundtrips() -> None:
    ts = HybridTimestamp(42, 7, NodeId("eu", "leader"))
    assert HybridTimestamp.decode(ts.encoded) == ts


def test_now_is_strictly_monotone_even_when_physical_stalls() -> None:
    clock = ManualClock(1000)
    hlc = HybridLogicalClock(NodeId("a", "1"), clock)
    stamps = [hlc.now() for _ in range(5)]
    # physical never moved, so logical must climb every time.
    for prev, cur in itertools.pairwise(stamps):
        assert cur > prev
    assert [s.logical for s in stamps] == [0, 1, 2, 3, 4]


def test_now_adopts_advancing_physical_time_and_resets_logical() -> None:
    clock = ManualClock(1000)
    hlc = HybridLogicalClock(NodeId("a", "1"), clock)
    hlc.now()
    hlc.now()  # logical = 1
    clock.advance(50)
    ts = hlc.now()
    assert ts.wall_ms == 1050
    assert ts.logical == 0


def test_recv_preserves_causality_send_before_recv() -> None:
    sender_clock = ManualClock(2000)
    receiver_clock = ManualClock(1000)
    sender = HybridLogicalClock(NodeId("a", "1"), sender_clock)
    receiver = HybridLogicalClock(NodeId("b", "1"), receiver_clock)
    sent = sender.send()
    got = receiver.recv(sent)
    # the received stamp strictly dominates the sent one (a -> b => ts(a) < ts(b)).
    assert got > sent


def test_recv_strictly_dominates_local_history() -> None:
    clock = ManualClock(1000)
    a = HybridLogicalClock(NodeId("a", "1"), clock)
    local = a.now()
    remote = HybridTimestamp(1000, 99, NodeId("z", "9"))
    got = a.recv(remote)
    assert got > local
    assert got > remote


def test_recv_clamps_a_remote_clock_far_in_the_future() -> None:
    clock = ManualClock(1000)
    a = HybridLogicalClock(NodeId("a", "1"), clock, max_skew_ms=100)
    # remote claims to be an hour ahead; we must not adopt that as our wall.
    poisoned = HybridTimestamp(1000 + 3_600_000, 0, NodeId("z", "9"))
    got = a.recv(poisoned)
    assert got.wall_ms == 1000  # clamped to local physical time
    assert got.logical >= 1  # but still strictly advanced


def test_recv_within_skew_bound_is_adopted() -> None:
    clock = ManualClock(1000)
    a = HybridLogicalClock(NodeId("a", "1"), clock, max_skew_ms=DEFAULT_MAX_SKEW_MS)
    near_future = HybridTimestamp(1000 + 50, 0, NodeId("z", "9"))
    got = a.recv(near_future)
    assert got.wall_ms == 1050


def test_manualclock_rejects_negative_advance() -> None:
    clock = ManualClock(0)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_manualclock_set_can_move_backwards() -> None:
    clock = ManualClock(5000)
    clock.set(10)
    assert clock.now_ms == 10


def test_peek_does_not_advance() -> None:
    hlc = HybridLogicalClock(NodeId("a", "1"), ManualClock(100))
    hlc.now()
    snap = hlc.peek()
    assert hlc.peek() == snap  # idempotent
