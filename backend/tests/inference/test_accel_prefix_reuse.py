"""Prefix / KV reuse tests — trie correctness + block accounting + eviction."""

from __future__ import annotations

import pytest

from app.inference.accel.metrics import PrefixReuseMetrics
from app.inference.accel.prefix_reuse import KVReuseBook, PrefixTrie


def _toks(n: int, prefix: str = "t") -> tuple[str, ...]:
    return tuple(f"{prefix}{i}" for i in range(n))


# --------------------------------------------------------------------------- #
# PrefixTrie
# --------------------------------------------------------------------------- #


def test_trie_longest_prefix() -> None:
    trie = PrefixTrie()
    trie.register(["a", "b", "c"])
    trie.register(["a", "b"])
    assert trie.longest_prefix(["a", "b", "c", "d"]) == 3
    assert trie.longest_prefix(["a", "b", "x"]) == 2
    assert trie.longest_prefix(["a", "z"]) == 0  # 'a' alone not registered terminal
    assert trie.longest_prefix(["q"]) == 0


def test_trie_terminal_only_matches() -> None:
    trie = PrefixTrie()
    trie.register(["a", "b", "c"])
    # 'a','b' are interior nodes, not terminals -> no reusable terminal there.
    assert trie.longest_prefix(["a", "b"]) == 0
    assert trie.longest_prefix(["a", "b", "c"]) == 3


def test_trie_refcount_and_release() -> None:
    trie = PrefixTrie()
    trie.register(["a", "b"])
    trie.register(["a", "b"])  # second ref
    assert trie.registered == 2
    assert trie.release(["a", "b"]) is True
    assert trie.longest_prefix(["a", "b", "c"]) == 2  # still resident (1 ref)
    assert trie.release(["a", "b"]) is True
    assert trie.longest_prefix(["a", "b", "c"]) == 0  # fully released
    assert trie.release(["a", "b"]) is False  # nothing left


def test_trie_release_prunes_nodes() -> None:
    trie = PrefixTrie()
    trie.register(["x", "y", "z"])
    trie.release(["x", "y", "z"])
    # internal structure pruned -> a fresh longest_prefix walks nothing
    assert trie.longest_prefix(["x", "y", "z"]) == 0
    assert trie.registered == 0


def test_trie_release_unknown() -> None:
    trie = PrefixTrie()
    assert trie.release(["nope"]) is False


# --------------------------------------------------------------------------- #
# KVReuseBook block accounting
# --------------------------------------------------------------------------- #


def test_plan_no_resident_prefix_is_all_fresh() -> None:
    book = KVReuseBook(block_size=4, metrics=PrefixReuseMetrics())
    plan = book.plan(_toks(10))
    assert plan.prompt_tokens == 10
    assert plan.reused_tokens == 0
    assert plan.recomputed_tokens == 10
    assert plan.blocks_reused == 0
    assert plan.blocks_allocated == 3  # ceil(10/4)
    assert plan.reuse_fraction == 0.0


def test_plan_reuses_whole_blocks_only() -> None:
    book = KVReuseBook(block_size=4)
    shared = _toks(10)  # register a 10-token prefix
    book.register(shared)
    # A prompt sharing the full 10-token prefix + 6 new tokens.
    prompt = shared + _toks(6, prefix="s")
    plan = book.plan(prompt)
    assert plan.prompt_tokens == 16
    # longest prefix = 10 tokens, but only floor(10/4)=2 full blocks (8 tokens)
    # are reusable; the partial 3rd block mixes prefix+suffix -> recomputed.
    assert plan.blocks_reused == 2
    assert plan.reused_tokens == 8
    assert plan.recomputed_tokens == 8
    assert plan.blocks_allocated == 4 - 2  # ceil(16/4)=4 total, minus 2 reused
    assert plan.reuse_fraction == 0.5


def test_block_aligned_prefix_full_reuse() -> None:
    book = KVReuseBook(block_size=4)
    shared = _toks(8)  # exactly 2 blocks
    book.register(shared)
    plan = book.plan(shared + _toks(4, prefix="s"))
    assert plan.blocks_reused == 2
    assert plan.reused_tokens == 8


def test_plan_records_metrics() -> None:
    metrics = PrefixReuseMetrics()
    book = KVReuseBook(block_size=4, metrics=metrics)
    book.register(_toks(8))
    book.plan(_toks(8) + _toks(8, prefix="s"))
    snap = metrics.snapshot()
    assert snap.requests == 1
    assert snap.prompt_tokens_total == 16
    assert snap.prompt_tokens_reused == 8
    assert snap.reuse_rate == 0.5


def test_plan_and_register_chains() -> None:
    book = KVReuseBook(block_size=4)
    sys_prompt = _toks(8, prefix="sys")
    # Register the *shared* prefix as its own terminal (the realistic pattern:
    # the known-common system/canon scaffold is registered once).
    book.register(sys_prompt)
    # first full prompt: only the system prefix is reusable (2 blocks)
    p1 = book.plan_and_register(sys_prompt + _toks(4, prefix="a"))
    assert p1.blocks_reused == 2
    # second call sharing the system prompt: still reuses its 2 blocks
    p2 = book.plan_and_register(sys_prompt + _toks(4, prefix="b"))
    assert p2.blocks_reused == 2


def test_empty_prompt() -> None:
    book = KVReuseBook(block_size=4)
    plan = book.plan(())
    assert plan.prompt_tokens == 0
    assert plan.blocks_allocated == 0
    assert plan.reuse_fraction == 0.0


def test_capacity_eviction_lru() -> None:
    # block_size 4, capacity 4 blocks total -> room for two 8-token (2-block)
    # prefixes; a third forces eviction of the least-recently-used.
    book = KVReuseBook(block_size=4, capacity_blocks=4)
    a = _toks(8, prefix="a")
    b = _toks(8, prefix="b")
    c = _toks(8, prefix="c")
    book.register(a)
    book.register(b)
    assert book.resident_blocks == 4
    book.touch(a)  # a is now most-recently-used; b is LRU
    book.register(c)  # evicts b
    assert book.resident_blocks == 4
    # a and c resident, b evicted
    assert book.plan(a + _toks(1, "x")).blocks_reused == 2
    assert book.plan(c + _toks(1, "x")).blocks_reused == 2
    assert book.plan(b + _toks(1, "x")).blocks_reused == 0


def test_invalid_block_size() -> None:
    with pytest.raises(ValueError):
        KVReuseBook(block_size=0)
