"""Token-budget bin-packing for continuous / in-flight batching.

A continuous-batching serving engine does not run requests one at a time; it
co-schedules many sequences and steps them together, bounded by a **token
budget** (the KV cache) and a **slot count** (max concurrent sequences) — not by
a request count. This module packs a priority-ordered stream of ready requests
into a micro-batch that fits a worker's *remaining* headroom, honouring three
constraints that make in-flight batching behave:

* **Token budget.** ``sum(total_tokens) <= token_budget``.
* **Slot budget.** ``len(batch) <= slot_budget``.
* **Prefill-chunk budget.** a per-step cap on *new prompt* tokens so a single
  huge prefill cannot stall the decode of everything already running (the
  "chunked prefill" discipline that keeps inter-token latency flat).

The packer is **order-preserving within a priority**: it never reorders the
input to chase a tighter pack (that would invert the fair-share ordering the
scheduler just produced). It greedily admits in arrival order, *skipping* a
request that does not fit the remaining budget so a smaller later request can
still ride along — bounded look-ahead, never a full knapsack, so it is O(n) and
deterministic. A request larger than the whole budget is reported as
``oversized`` rather than silently dropped, so the router can route it to an
empty worker or reject it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .errors import RouterConfigError
from .request import InferenceRequest


@dataclass(frozen=True, slots=True)
class BatchBudget:
    """The headroom a micro-batch must fit into.

    Attributes:
        token_budget: Remaining KV tokens on the target worker.
        slot_budget: Remaining concurrent-sequence slots.
        prefill_chunk_budget: Max *new prompt* tokens admitted in this step;
            ``None`` disables the chunked-prefill cap.
    """

    token_budget: int
    slot_budget: int
    prefill_chunk_budget: int | None = None

    def __post_init__(self) -> None:
        if self.token_budget < 0:
            raise RouterConfigError("token_budget must be non-negative")
        if self.slot_budget < 0:
            raise RouterConfigError("slot_budget must be non-negative")
        if self.prefill_chunk_budget is not None and self.prefill_chunk_budget < 0:
            raise RouterConfigError("prefill_chunk_budget must be non-negative when set")


@dataclass(slots=True)
class PackResult:
    """The outcome of one bin-pack pass.

    Attributes:
        batch: Requests admitted to the micro-batch, in input order.
        deferred: Requests that fit the *worker* but not *this* step's remaining
            budget — the router re-offers them next tick.
        oversized: Requests too large for the full budget even when empty — the
            router must route them elsewhere or reject them (never deferrable).
        tokens_used / slots_used / prefill_used: What the batch consumed.
    """

    batch: list[InferenceRequest] = field(default_factory=list)
    deferred: list[InferenceRequest] = field(default_factory=list)
    oversized: list[InferenceRequest] = field(default_factory=list)
    tokens_used: int = 0
    slots_used: int = 0
    prefill_used: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.batch


class TokenBinPacker:
    """Greedy, order-preserving token-budget packer for in-flight batching."""

    def __init__(self, *, lookahead: int | None = None) -> None:
        """``lookahead`` bounds how far past a non-fitting request the packer
        scans for a smaller one that still fits. ``None`` scans the whole input
        (full first-fit-decreasing-by-arrival); a small value keeps packing
        nearly head-of-line so a backlog of tiny requests can't perpetually hop
        the queue ahead of a medium one that just barely doesn't fit.
        """
        if lookahead is not None and lookahead < 0:
            raise RouterConfigError("lookahead must be non-negative when set")
        self._lookahead = lookahead

    def pack(self, requests: Sequence[InferenceRequest], budget: BatchBudget) -> PackResult:
        """Pack as many requests as fit into ``budget``, preserving input order."""
        result = PackResult()
        tokens_left = budget.token_budget
        slots_left = budget.slot_budget
        prefill_left = budget.prefill_chunk_budget
        skipped_since_fit = 0

        for req in requests:
            need_tokens = req.total_tokens
            need_prefill = req.prompt_tokens

            # Oversized: cannot fit even an empty worker's full budget.
            never_fits = (
                need_tokens > budget.token_budget
                or budget.slot_budget < 1
                or (
                    budget.prefill_chunk_budget is not None
                    and need_prefill > budget.prefill_chunk_budget
                )
            )
            if never_fits:
                result.oversized.append(req)
                continue

            fits_now = (
                slots_left >= 1
                and need_tokens <= tokens_left
                and (prefill_left is None or need_prefill <= prefill_left)
            )
            if fits_now:
                result.batch.append(req)
                tokens_left -= need_tokens
                slots_left -= 1
                if prefill_left is not None:
                    prefill_left -= need_prefill
                result.tokens_used += need_tokens
                result.slots_used += 1
                result.prefill_used += need_prefill
                skipped_since_fit = 0
                if slots_left == 0 or tokens_left == 0:
                    # Budget exhausted: everything remaining defers.
                    result.deferred.extend(self._tail(requests, req))
                    break
            else:
                result.deferred.append(req)
                skipped_since_fit += 1
                if self._lookahead is not None and skipped_since_fit > self._lookahead:
                    # Stop hopping; defer the rest to preserve near-FIFO order.
                    remaining = self._tail(requests, req)
                    result.deferred.extend(remaining)
                    break
        return result

    @staticmethod
    def _tail(
        requests: Sequence[InferenceRequest], after: InferenceRequest
    ) -> list[InferenceRequest]:
        """Requests strictly after ``after`` in the sequence (by identity)."""
        out: list[InferenceRequest] = []
        seen = False
        for r in requests:
            if seen:
                out.append(r)
            elif r is after:
                seen = True
        return out


def total_tokens(requests: Iterable[InferenceRequest]) -> int:
    """Sum of worst-case token footprints (small helper for metrics/tests)."""
    return sum(r.total_tokens for r in requests)


__all__ = ["BatchBudget", "PackResult", "TokenBinPacker", "total_tokens"]
