"""Prompt-prefix / KV-cache reuse bookkeeping.

Long Kinora prompts share large common prefixes — the same system prompt, the
same canon excerpt, the same few-shot scaffold — across many calls. A serving
engine that keeps the attention KV-cache for a prefix can *skip recomputing* it
on the next request that shares it; only the novel suffix is processed. This
module is the accounting + decision layer for that reuse: it does **not** itself
hold GPU KV memory (the gateway transport does), it tracks *which* prefixes are
resident, decides how much of an incoming prompt can be served from a resident
prefix, and reports the savings.

Two structures:

* :class:`PrefixTrie` — a token-level radix-ish trie of registered prefixes.
  ``longest_prefix(tokens)`` returns the longest registered prefix that is a
  prefix of ``tokens`` (the reusable span). Registration is reference-counted so
  shared prefixes are not evicted while still in use.
* :class:`KVReuseBook` — turns token spans into *block* arithmetic (KV is
  allocated in fixed-size blocks, like paged-attention) and records, per request,
  how many prompt tokens / blocks were reused vs freshly allocated. It enforces a
  block-capacity budget with LRU eviction of unreferenced prefixes.

All deterministic; the block model mirrors paged-attention so the numbers are
faithful, but nothing here touches a real device.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .metrics import PrefixReuseMetrics


@dataclass(slots=True)
class _TrieNode:
    children: dict[str, _TrieNode] = field(default_factory=dict)
    #: Number of registered prefixes that *end* at this node.
    terminal_refs: int = 0
    #: Monotonic tick of last use (for LRU eviction at the engine layer).
    last_used: int = 0


class PrefixTrie:
    """Token-level trie of registered, reference-counted prompt prefixes."""

    __slots__ = ("_clock_tick", "_root", "_size")

    def __init__(self) -> None:
        self._root = _TrieNode()
        self._size = 0  # number of distinct registered prefixes (by ref)
        self._clock_tick = 0

    @property
    def registered(self) -> int:
        """Total reference count across all registered prefixes."""
        return self._size

    def _tick(self) -> int:
        self._clock_tick += 1
        return self._clock_tick

    def register(self, tokens: Sequence[str]) -> None:
        """Register ``tokens`` as a resident prefix (ref-counted, idempotent-ish)."""
        node = self._root
        tick = self._tick()
        node.last_used = tick
        for tok in tokens:
            node = node.children.setdefault(tok, _TrieNode())
            node.last_used = tick
        node.terminal_refs += 1
        self._size += 1

    def release(self, tokens: Sequence[str]) -> bool:
        """Decrement the reference for a registered prefix. Returns released?."""
        path: list[tuple[_TrieNode, str]] = []
        node = self._root
        for tok in tokens:
            child = node.children.get(tok)
            if child is None:
                return False
            path.append((node, tok))
            node = child
        if node.terminal_refs <= 0:
            return False
        node.terminal_refs -= 1
        self._size -= 1
        # Prune now-empty, unreferenced tail nodes.
        cur = node
        for parent, tok in reversed(path):
            if cur.children or cur.terminal_refs > 0:
                break
            del parent.children[tok]
            cur = parent
        return True

    def longest_prefix(self, tokens: Sequence[str]) -> int:
        """Length of the longest *registered* prefix that prefixes ``tokens``.

        Walks the trie along ``tokens`` and returns the deepest position that is
        a terminal of some registered prefix (touching nodes updates LRU ticks).
        """
        node = self._root
        tick = self._tick()
        best = 0
        for depth, tok in enumerate(tokens, start=1):
            child = node.children.get(tok)
            if child is None:
                break
            child.last_used = tick
            node = child
            if node.terminal_refs > 0:
                best = depth
        return best


@dataclass(frozen=True, slots=True)
class ReusePlan:
    """How an incoming prompt maps onto reused vs recomputed KV blocks."""

    prompt_tokens: int
    reused_tokens: int
    recomputed_tokens: int
    blocks_reused: int
    blocks_allocated: int
    block_size: int

    @property
    def reuse_fraction(self) -> float:
        return self.reused_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


class KVReuseBook:
    """Block-level KV reuse accounting over a :class:`PrefixTrie`.

    KV memory is paged into ``block_size``-token blocks. A resident prefix of
    length ``L`` covers ``L // block_size`` *full* blocks that can be shared; the
    partial trailing block must always be recomputed (it mixes prefix + suffix
    tokens). ``capacity_blocks`` bounds total resident blocks; registering a new
    prefix that would overflow evicts least-recently-used unreferenced prefixes.
    """

    def __init__(
        self,
        *,
        block_size: int = 16,
        capacity_blocks: int | None = None,
        metrics: PrefixReuseMetrics | None = None,
    ) -> None:
        if block_size < 1:
            raise ValueError("block_size must be >= 1")
        self._block_size = block_size
        self._capacity = capacity_blocks
        self._trie = PrefixTrie()
        self._metrics = metrics or PrefixReuseMetrics()
        # registered prefix tokens -> (blocks, last_used) for capacity/LRU.
        self._resident: dict[tuple[str, ...], int] = {}
        self._lru: dict[tuple[str, ...], int] = {}
        self._tick = 0

    @property
    def metrics(self) -> PrefixReuseMetrics:
        return self._metrics

    @property
    def block_size(self) -> int:
        return self._block_size

    @property
    def resident_blocks(self) -> int:
        return sum(self._resident.values())

    def _full_blocks(self, n_tokens: int) -> int:
        return n_tokens // self._block_size

    def plan(self, prompt_tokens: Sequence[str]) -> ReusePlan:
        """Compute the reuse plan for ``prompt_tokens`` WITHOUT registering it.

        Records the request in metrics. The reusable span is the longest
        registered prefix, rounded *down* to a whole number of blocks (a partial
        block at the prefix boundary cannot be shared).
        """
        prompt = tuple(prompt_tokens)
        n = len(prompt)
        prefix_len = self._trie.longest_prefix(prompt)
        reusable_blocks = self._full_blocks(prefix_len)
        reused_tokens = reusable_blocks * self._block_size
        recomputed_tokens = n - reused_tokens
        total_blocks = math.ceil(n / self._block_size) if n else 0
        blocks_allocated = total_blocks - reusable_blocks
        plan = ReusePlan(
            prompt_tokens=n,
            reused_tokens=reused_tokens,
            recomputed_tokens=recomputed_tokens,
            blocks_reused=reusable_blocks,
            blocks_allocated=blocks_allocated,
            block_size=self._block_size,
        )
        self._metrics.record_request(
            prompt_tokens=n,
            reused_tokens=reused_tokens,
            blocks_reused=reusable_blocks,
            blocks_allocated=blocks_allocated,
        )
        return plan

    def register(self, tokens: Sequence[str]) -> None:
        """Mark ``tokens`` resident so later prompts can reuse it; evicts to fit.

        Reuse keys on *terminal* prefixes: a later prompt reuses ``tokens`` only
        if ``tokens`` is a prefix of it. To share a system/canon scaffold across
        many prompts, register that scaffold span itself (not just whole
        prompts) — registering a full prompt only helps an identical re-read.
        """
        key = tuple(tokens)
        blocks = math.ceil(len(key) / self._block_size) if key else 0
        self._trie.register(key)
        self._resident[key] = blocks
        self._tick += 1
        self._lru[key] = self._tick
        self._evict_to_capacity()

    def touch(self, tokens: Sequence[str]) -> None:
        """Refresh LRU for a resident prefix (call on a reuse hit)."""
        key = tuple(tokens)
        if key in self._lru:
            self._tick += 1
            self._lru[key] = self._tick

    def _evict_to_capacity(self) -> None:
        if self._capacity is None:
            return
        while self.resident_blocks > self._capacity and self._lru:
            # Evict least-recently-used resident prefix.
            victim = min(self._lru, key=lambda k: self._lru[k])
            self._trie.release(victim)
            self._resident.pop(victim, None)
            self._lru.pop(victim, None)

    def plan_and_register(self, prompt_tokens: Sequence[str]) -> ReusePlan:
        """Plan reuse for a prompt then register it as resident (the common path)."""
        plan = self.plan(prompt_tokens)
        self.register(prompt_tokens)
        return plan


__all__ = ["KVReuseBook", "PrefixTrie", "ReusePlan"]
