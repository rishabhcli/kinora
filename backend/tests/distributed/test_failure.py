"""Tests for partition detection and healing (failure.py)."""

from __future__ import annotations

from app.distributed.replication.clock import NodeId
from app.distributed.replication.failure import (
    FailureDetector,
    PartitionEventKind,
    PartitionMonitor,
    PeerState,
    PhiDetector,
)

US = NodeId("us", "1")
EU = NodeId("eu", "1")
AP = NodeId("ap", "1")


def test_unseen_peer_is_suspected() -> None:
    fd = FailureDetector(timeout_ms=1000)
    assert fd.state(US, now_ms=0) is PeerState.SUSPECTED
    assert not fd.is_alive(US, now_ms=0)


def test_recent_heartbeat_is_alive() -> None:
    fd = FailureDetector(timeout_ms=1000)
    fd.heartbeat(US, at_ms=500)
    assert fd.is_alive(US, now_ms=1000)  # 500ms < 1000 timeout
    assert not fd.is_alive(US, now_ms=2000)  # 1500ms > timeout


def test_out_of_order_heartbeats_keep_latest() -> None:
    fd = FailureDetector(timeout_ms=1000)
    fd.heartbeat(US, at_ms=900)
    fd.heartbeat(US, at_ms=500)  # stale, ignored
    assert fd.last_seen(US) == 900


def test_alive_peers_filters() -> None:
    fd = FailureDetector(timeout_ms=1000)
    fd.heartbeat(US, 100)
    fd.heartbeat(EU, 100)
    # AP never beats
    assert fd.alive_peers([US, EU, AP], now_ms=500) == {US, EU}


def test_phi_rises_with_silence() -> None:
    phi = PhiDetector(min_std_ms=10.0)
    # establish a steady 100ms cadence
    for t in range(0, 1001, 100):
        phi.heartbeat(US, t)
    near = phi.phi(US, now_ms=1050)  # 50ms after last, on cadence
    far = phi.phi(US, now_ms=2000)  # 1000ms of silence
    assert far > near
    assert phi.is_available(US, now_ms=1050)
    assert not phi.is_available(US, now_ms=5000)


def test_phi_unseen_peer_is_infinite() -> None:
    phi = PhiDetector()
    assert phi.phi(US, now_ms=0) == float("inf")
    assert not phi.is_available(US, now_ms=0)


def test_monitor_emits_partition_then_heal() -> None:
    mon = PartitionMonitor([US, EU], detector=FailureDetector(timeout_ms=1000))
    mon.heartbeat(US, 0)
    mon.heartbeat(EU, 0)
    # everyone alive, no transitions while fresh
    assert mon.observe(now_ms=500) == []
    # US goes silent; at 2000ms it exceeds timeout -> PARTITIONED
    mon.heartbeat(EU, 1800)
    events = mon.observe(now_ms=2000)
    kinds = {(e.peer, e.kind) for e in events}
    assert (US, PartitionEventKind.PARTITIONED) in kinds
    assert mon.current_state(US) is PeerState.SUSPECTED
    assert mon.current_state(EU) is PeerState.ALIVE
    # US comes back -> HEALED
    mon.heartbeat(US, 2500)
    healed = mon.observe(now_ms=2600)
    assert [(e.peer, e.kind) for e in healed] == [(US, PartitionEventKind.HEALED)]
    assert US in mon.healthy_peers()


def test_monitor_no_duplicate_events_while_stable() -> None:
    mon = PartitionMonitor([US], detector=FailureDetector(timeout_ms=1000))
    mon.heartbeat(US, 0)
    mon.observe(now_ms=2000)  # PARTITIONED
    # still silent -> no repeat event
    assert mon.observe(now_ms=3000) == []
