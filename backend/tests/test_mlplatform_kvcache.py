"""Unit + property tests for the paged KV-cache (no infra)."""

from __future__ import annotations

import contextlib

import pytest

from app.mlplatform.serving.contracts import _seeded_unit
from app.mlplatform.serving.errors import (
    CapacityError,
    InvariantViolationError,
    ServingConfigError,
)
from app.mlplatform.serving.kvcache import PagedKVCache, PagedKVConfig


def test_config_validation() -> None:
    with pytest.raises(ServingConfigError):
        PagedKVConfig(total_blocks=0)
    with pytest.raises(ServingConfigError):
        PagedKVConfig(total_blocks=4, block_tokens=0)


def test_blocks_for_ceil_division() -> None:
    cfg = PagedKVConfig(total_blocks=100, block_tokens=16)
    assert cfg.blocks_for(0) == 0
    assert cfg.blocks_for(1) == 1
    assert cfg.blocks_for(16) == 1
    assert cfg.blocks_for(17) == 2
    assert cfg.blocks_for(32) == 2


def test_admit_grow_free_accounting() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=10, block_tokens=16))
    assert cache.free_blocks == 10
    cache.admit("r1", prompt_tokens=32)  # 2 blocks
    assert cache.used_blocks == 2
    assert cache.blocks_held("r1") == 2
    added = cache.grow("r1", new_context_tokens=48)  # → 3 blocks
    assert added == 1
    assert cache.used_blocks == 3
    freed = cache.free("r1")
    assert freed == 3
    assert cache.used_blocks == 0
    assert cache.free_blocks == 10


def test_admit_double_raises() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=10))
    cache.admit("r1", 16)
    with pytest.raises(InvariantViolationError, match="already has an allocation"):
        cache.admit("r1", 16)


def test_capacity_error_on_overcommit() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=2, block_tokens=16))
    assert cache.can_admit(48) is False  # needs 3 blocks, only 2
    with pytest.raises(CapacityError):
        cache.admit("big", 48)


def test_grow_unknown_request_raises() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=4))
    with pytest.raises(InvariantViolationError, match="no allocation to grow"):
        cache.grow("ghost", 16)


def test_grow_capacity_error() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=2, block_tokens=16))
    cache.admit("r1", 16)  # 1 block
    cache.admit("r2", 16)  # 1 block, pool full
    with pytest.raises(CapacityError):
        cache.grow("r1", 32)  # would need a 2nd block, none free


def test_free_is_idempotent() -> None:
    cache = PagedKVCache(PagedKVConfig(total_blocks=4))
    assert cache.free("never-admitted") == 0


def test_prefix_reuse_shares_blocks() -> None:
    cfg = PagedKVConfig(total_blocks=20, block_tokens=16, enable_prefix_reuse=True)
    cache = PagedKVCache(cfg)
    # First request lays down the shared prefix blocks.
    new1 = cache.admit("r1", prompt_tokens=64, prefix="canon-slice")  # 4 blocks
    assert new1 == 4
    used_after_first = cache.used_blocks
    # Second request with the same prefix reuses all 4 prefix blocks → 0 new.
    new2 = cache.admit("r2", prompt_tokens=64, prefix="canon-slice")
    assert new2 == 0
    assert cache.used_blocks == used_after_first  # no new physical blocks
    assert cache.reused_blocks_total == 4


def test_prefix_reuse_refcount_release() -> None:
    cfg = PagedKVConfig(total_blocks=20, block_tokens=16)
    cache = PagedKVCache(cfg)
    cache.admit("r1", 32, prefix="p")  # 2 shared blocks
    cache.admit("r2", 32, prefix="p")  # reuses both
    assert cache.used_blocks == 2
    # Freeing r1 only drops refcounts; physical blocks survive for r2.
    freed = cache.free("r1")
    assert freed == 0
    assert cache.used_blocks == 2
    # Freeing r2 reclaims them.
    assert cache.free("r2") == 2
    assert cache.used_blocks == 0


def test_prefix_reuse_disabled() -> None:
    cfg = PagedKVConfig(total_blocks=20, block_tokens=16, enable_prefix_reuse=False)
    cache = PagedKVCache(cfg)
    cache.admit("r1", 32, prefix="p")
    cache.admit("r2", 32, prefix="p")
    assert cache.used_blocks == 4  # no sharing
    assert cache.reused_blocks_total == 0


# -- property-style sweeps ------------------------------------------------- #


@pytest.mark.parametrize("seed_i", range(40))
def test_property_used_plus_free_equals_capacity(seed_i: int) -> None:
    """Invariant across many randomized admit/grow/free sequences:

    ``used_blocks + free_blocks == capacity`` and ``used_blocks <= capacity`` at
    every step. Operations are seeded so the sweep is reproducible.
    """
    cap = 8 + int(_seeded_unit("cap", str(seed_i)) * 24)
    cfg = PagedKVConfig(total_blocks=cap, block_tokens=16)
    cache = PagedKVCache(cfg)
    live: list[str] = []
    for op_i in range(60):
        assert cache.used_blocks + cache.free_blocks == cache.capacity
        assert 0 <= cache.used_blocks <= cache.capacity
        u = _seeded_unit("op", str(seed_i), str(op_i))
        if u < 0.45 or not live:
            rid = f"r{seed_i}-{op_i}"
            tokens = 1 + int(_seeded_unit("tok", str(seed_i), str(op_i)) * 80)
            try:
                cache.admit(rid, tokens)
                live.append(rid)
            except CapacityError:
                pass  # legitimately full — invariant must still hold
        elif u < 0.75:
            rid = live[op_i % len(live)]
            grow_to = cache.blocks_held(rid) * 16 + 16
            with contextlib.suppress(CapacityError):
                cache.grow(rid, grow_to)
        else:
            rid = live.pop(op_i % len(live))
            cache.free(rid)
    # Drain everything; the cache must return to empty exactly.
    for rid in live:
        cache.free(rid)
    assert cache.used_blocks == 0
    assert cache.free_blocks == cache.capacity
