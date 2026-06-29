"""Unit tests for the shard topology (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.topology import Shard, ShardState, ShardTopology


def _shard(sid: str, state: ShardState = ShardState.ACTIVE, weight: int = 1) -> Shard:
    return Shard(id=sid, primary_url=f"postgresql+asyncpg://h/{sid}", state=state, weight=weight)


def test_state_access_matrix() -> None:
    assert ShardState.ACTIVE.accepts_writes and ShardState.ACTIVE.accepts_reads
    assert not ShardState.READ_ONLY.accepts_writes and ShardState.READ_ONLY.accepts_reads
    assert not ShardState.DRAINING.accepts_writes and ShardState.DRAINING.accepts_reads
    assert not ShardState.OFFLINE.accepts_writes and not ShardState.OFFLINE.accepts_reads


def test_shard_validation() -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Shard(id="", primary_url="x")
    with pytest.raises(ValueError, match="primary_url"):
        Shard(id="s1", primary_url="")
    with pytest.raises(ValueError, match="weight"):
        Shard(id="s1", primary_url="x", weight=0)


def test_topology_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate shard id"):
        ShardTopology.of(_shard("s1"), _shard("s1"))


def test_topology_lookups() -> None:
    topo = ShardTopology.of(_shard("s1"), _shard("s2"), _shard("s3"))
    assert topo.ids == ("s1", "s2", "s3")
    assert topo.get("s2").id == "s2"
    assert topo.has("s3") and not topo.has("s9")
    assert "s1" in topo and "nope" not in topo
    assert len(topo) == 3
    with pytest.raises(KeyError):
        topo.get("missing")


def test_state_filters() -> None:
    topo = ShardTopology.of(
        _shard("a", ShardState.ACTIVE),
        _shard("b", ShardState.READ_ONLY),
        _shard("c", ShardState.DRAINING),
        _shard("d", ShardState.OFFLINE),
    )
    assert {s.id for s in topo.active()} == {"a"}
    assert {s.id for s in topo.writable()} == {"a"}
    assert {s.id for s in topo.readable()} == {"a", "b", "c"}


def test_pure_transitions_do_not_mutate_original() -> None:
    topo = ShardTopology.of(_shard("s1"))
    grown = topo.with_shard(_shard("s2"))
    assert topo.ids == ("s1",)
    assert grown.ids == ("s1", "s2")

    drained = grown.with_state("s2", ShardState.DRAINING)
    assert grown.get("s2").state is ShardState.ACTIVE
    assert drained.get("s2").state is ShardState.DRAINING

    shrunk = drained.without_shard("s1")
    assert shrunk.ids == ("s2",)
    assert drained.ids == ("s1", "s2")


def test_with_shard_rejects_existing() -> None:
    topo = ShardTopology.of(_shard("s1"))
    with pytest.raises(ValueError, match="already in topology"):
        topo.with_shard(_shard("s1"))


def test_without_shard_rejects_absent() -> None:
    topo = ShardTopology.of(_shard("s1"))
    with pytest.raises(KeyError):
        topo.without_shard("nope")


def test_replace_shard() -> None:
    topo = ShardTopology.of(_shard("s1", weight=1))
    replaced = topo.replace_shard(_shard("s1", weight=5))
    assert replaced.get("s1").weight == 5
    assert topo.get("s1").weight == 1
