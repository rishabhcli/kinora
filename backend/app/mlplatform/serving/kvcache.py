"""Paged KV-cache with block-level prefix reuse (a vLLM-style PagedAttention model).

The KV-cache is the memory bottleneck of LLM serving. A naive server reserves a
contiguous slab sized for every sequence's *maximum* length, wasting most of it.
PagedAttention instead chops the cache into fixed-size **blocks** (``block_tokens``
tokens each) and hands a sequence blocks on demand, like virtual-memory pages. Two
consequences this module models faithfully:

* **No external fragmentation.** A sequence needs ``ceil(ctx / block_tokens)``
  blocks; any free block can serve any sequence. Admission is a simple block-count
  check against a fixed pool.
* **Prefix reuse (KV-cache reuse).** Two requests that share a prompt prefix can
  share the blocks covering that prefix. We model this with content-addressed
  blocks: a block is keyed by the hash of the token span it covers, and identical
  spans map to the same physical block via reference counting. A reused block costs
  zero new allocation and — crucially — its prefill compute can be skipped.

Everything is integer block accounting; there is no real tensor memory. The cache
exposes the invariants the property tests assert: ``used + free == capacity`` at all
times, and ``used`` never exceeds capacity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.mlplatform.serving.errors import CapacityError, InvariantViolationError, ServingConfigError


@dataclass(frozen=True, slots=True)
class PagedKVConfig:
    """Sizing for the paged cache.

    ``total_blocks`` is the physical block pool. ``block_tokens`` is how many
    tokens one block holds. ``enable_prefix_reuse`` toggles content-addressed
    sharing (off → every sequence gets private blocks, the conservative baseline).
    """

    total_blocks: int
    block_tokens: int = 16
    enable_prefix_reuse: bool = True

    def __post_init__(self) -> None:
        if self.total_blocks <= 0:
            raise ServingConfigError("total_blocks must be positive")
        if self.block_tokens <= 0:
            raise ServingConfigError("block_tokens must be positive")

    def blocks_for(self, tokens: int) -> int:
        """Blocks required to hold ``tokens`` tokens (ceil division)."""
        if tokens <= 0:
            return 0
        return (tokens + self.block_tokens - 1) // self.block_tokens


@dataclass(slots=True)
class _Allocation:
    """The blocks currently held by one sequence."""

    request_id: str
    block_hashes: list[str] = field(default_factory=list)

    @property
    def n_blocks(self) -> int:
        return len(self.block_hashes)


class PagedKVCache:
    """A simulated paged KV-cache with reference-counted, content-addressed blocks.

    A "physical block" is just a hash with a reference count. ``used_blocks`` counts
    *distinct* physical blocks currently referenced; reuse means two sequences can
    reference the same physical block while ``used_blocks`` counts it once. Private
    (non-shared) blocks get unique hashes so they never collide.
    """

    def __init__(self, config: PagedKVConfig) -> None:
        self.config = config
        # physical block hash -> refcount
        self._refcount: dict[str, int] = {}
        # request_id -> its allocation
        self._allocs: dict[str, _Allocation] = {}
        self._reused_blocks_total = 0
        self._allocated_blocks_total = 0

    # -- accounting -------------------------------------------------------- #

    @property
    def capacity(self) -> int:
        return self.config.total_blocks

    @property
    def used_blocks(self) -> int:
        """Distinct physical blocks currently referenced."""
        return len(self._refcount)

    @property
    def free_blocks(self) -> int:
        return self.capacity - self.used_blocks

    @property
    def utilization(self) -> float:
        return self.used_blocks / self.capacity if self.capacity else 0.0

    @property
    def reused_blocks_total(self) -> int:
        """Cumulative blocks that were satisfied by an existing physical block."""
        return self._reused_blocks_total

    @property
    def allocated_blocks_total(self) -> int:
        """Cumulative *new* physical blocks allocated across the run."""
        return self._allocated_blocks_total

    def blocks_held(self, request_id: str) -> int:
        a = self._allocs.get(request_id)
        return a.n_blocks if a else 0

    # -- admission --------------------------------------------------------- #

    def can_admit(self, prompt_tokens: int, *, prefix: str | None = None) -> bool:
        """Whether a fresh sequence's prompt fits given current free blocks.

        Accounts for prefix reuse: blocks of the prompt that hash to already-resident
        physical blocks cost nothing.
        """
        needed = self._new_blocks_for_prompt(prompt_tokens, prefix=prefix)
        return needed <= self.free_blocks

    def admit(self, request_id: str, prompt_tokens: int, *, prefix: str | None = None) -> int:
        """Reserve blocks for a new sequence's prompt. Returns blocks newly allocated.

        Shared prefix blocks bump the existing physical block's refcount instead of
        allocating; the rest are private blocks with unique hashes. Raises
        :class:`CapacityError` if there is not enough room.
        """
        if request_id in self._allocs:
            raise InvariantViolationError(f"request {request_id!r} already has an allocation")
        n_blocks = self.config.blocks_for(prompt_tokens)
        prefix_blocks = self._prefix_covered_blocks(n_blocks, prefix=prefix)
        # New physical blocks needed = prefix blocks not yet resident + private blocks.
        new_blocks = sum(
            1
            for b in range(prefix_blocks)
            if self._prefix_block_hash(prefix or "", b) not in self._refcount
        ) + (n_blocks - prefix_blocks)
        if new_blocks > self.free_blocks:
            raise CapacityError(f"need {new_blocks} new blocks, only {self.free_blocks} free")
        alloc = _Allocation(request_id=request_id)
        # Prefix-covered blocks are content-addressed by the prefix key: a resident
        # block is reused (refcount bump), an absent one is allocated fresh and made
        # available for the next request that shares the prefix.
        for b in range(prefix_blocks):
            h = self._prefix_block_hash(prefix or "", b)
            if h in self._refcount:
                self._refcount[h] += 1
                self._reused_blocks_total += 1
            else:
                self._refcount[h] = 1
                self._allocated_blocks_total += 1
            alloc.block_hashes.append(h)
        # Private (per-request) blocks for any prompt tail beyond the prefix.
        for b in range(prefix_blocks, n_blocks):
            h = self._private_block_hash(request_id, b)
            self._refcount[h] = 1
            alloc.block_hashes.append(h)
            self._allocated_blocks_total += 1
        self._allocs[request_id] = alloc
        self._check_invariants()
        return new_blocks

    def grow(self, request_id: str, new_context_tokens: int) -> int:
        """Extend a sequence to ``new_context_tokens`` total. Returns blocks added.

        Called each decode step as the sequence's context lengthens. Growth always
        allocates private blocks (generated tokens are unique). Raises
        :class:`CapacityError` when the pool is exhausted (the caller must then evict
        or wait — this is exactly the preemption signal continuous batching needs).
        """
        alloc = self._allocs.get(request_id)
        if alloc is None:
            raise InvariantViolationError(f"request {request_id!r} has no allocation to grow")
        needed = self.config.blocks_for(new_context_tokens)
        have = alloc.n_blocks
        add = needed - have
        if add <= 0:
            return 0
        if add > self.free_blocks:
            raise CapacityError(f"grow needs {add} blocks, only {self.free_blocks} free")
        for b in range(have, needed):
            h = self._private_block_hash(request_id, b)
            self._refcount[h] = 1
            alloc.block_hashes.append(h)
            self._allocated_blocks_total += 1
        self._check_invariants()
        return add

    def free(self, request_id: str) -> int:
        """Release a sequence's blocks. Returns physical blocks actually freed.

        Shared blocks only drop a refcount; a physical block is reclaimed when its
        refcount hits zero. Idempotent: freeing an unknown request frees nothing.
        """
        alloc = self._allocs.pop(request_id, None)
        if alloc is None:
            return 0
        freed = 0
        for h in alloc.block_hashes:
            rc = self._refcount.get(h, 0)
            if rc <= 1:
                self._refcount.pop(h, None)
                freed += 1
            else:
                self._refcount[h] = rc - 1
        self._check_invariants()
        return freed

    # -- prefix-reuse internals -------------------------------------------- #

    def _new_blocks_for_prompt(self, prompt_tokens: int, *, prefix: str | None) -> int:
        """How many *new* physical blocks admitting this prompt would allocate.

        Prefix-covered blocks already resident cost nothing; the rest (absent prefix
        blocks + private tail blocks) each cost one new physical block.
        """
        n_blocks = self.config.blocks_for(prompt_tokens)
        prefix_blocks = self._prefix_covered_blocks(n_blocks, prefix=prefix)
        resident = sum(
            1
            for b in range(prefix_blocks)
            if self._prefix_block_hash(prefix or "", b) in self._refcount
        )
        return n_blocks - resident

    def _prefix_covered_blocks(self, n_blocks: int, *, prefix: str | None) -> int:
        """How many leading blocks of a prompt are addressed by the prefix key.

        Reuse is enabled only when configured and a ``prefix`` key is supplied. We
        treat the *whole* prompt as covered by the prefix key (the common Kinora
        case: many shots share the same canon-slice prompt). Prefix-covered blocks
        are content-addressed, so identical prompts across requests map to the same
        physical blocks and share them via reference counting.
        """
        if not self.config.enable_prefix_reuse or not prefix:
            return 0
        return n_blocks

    @staticmethod
    def _prefix_block_hash(prefix: str, block_index: int) -> str:
        raw = f"prefix\x1f{prefix}\x1f{block_index}".encode()
        return "p" + hashlib.sha1(raw).hexdigest()[:16]

    @staticmethod
    def _private_block_hash(request_id: str, block_index: int) -> str:
        raw = f"private\x1f{request_id}\x1f{block_index}".encode()
        return "x" + hashlib.sha1(raw).hexdigest()[:16]

    def _check_invariants(self) -> None:
        if self.used_blocks > self.capacity:
            raise InvariantViolationError(
                f"KV-cache used {self.used_blocks} > capacity {self.capacity}"
            )
        if self.used_blocks < 0:
            raise InvariantViolationError("KV-cache used went negative")


__all__ = ["PagedKVCache", "PagedKVConfig"]
