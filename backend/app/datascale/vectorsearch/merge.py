"""Result-set merging for the sharded / multi-source query path.

Each shard returns a list of :class:`SearchResult` already sorted closest-first
on the shared "smaller is closer" ordering key. Merging them into a single
global top-``k`` is therefore a k-way merge of sorted runs — :func:`merge_results`
uses a heap so it is ``O(total · log shards)`` and never materialises the full
cross-shard set.

De-duplication: an id can legitimately appear from only one shard under the
router contract, but the merge still de-dups defensively (keeping the closer
copy) so it is safe to reuse for non-disjoint sources (e.g. fusing a vector run
with a keyword run).
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence

from .types import SearchResult, VectorId


def merge_results(runs: Sequence[Sequence[SearchResult]], k: int) -> list[SearchResult]:
    """K-way merge of per-source sorted runs into the global closest-``k``.

    Each run must be sorted by ``distance`` ascending (the index guarantees it).
    Ties break deterministically by ``id`` so the output is stable across runs.
    """
    if k <= 0:
        return []
    # Heap of (distance, id, run_index, pos_in_run). id in the key makes ties
    # deterministic; run/pos let us advance the right run.
    heap: list[tuple[float, VectorId, int, int]] = []
    for ri, run in enumerate(runs):
        if run:
            r = run[0]
            heapq.heappush(heap, (r.distance, r.id, ri, 0))
    out: list[SearchResult] = []
    seen: set[VectorId] = set()
    while heap and len(out) < k:
        dist, _vid, ri, pos = heapq.heappop(heap)
        result = runs[ri][pos]
        if result.id not in seen:
            seen.add(result.id)
            out.append(result)
        nxt = pos + 1
        if nxt < len(runs[ri]):
            nr = runs[ri][nxt]
            heapq.heappush(heap, (nr.distance, nr.id, ri, nxt))
    return out


def merge_dedup_keep_closest(runs: Sequence[Sequence[SearchResult]], k: int) -> list[SearchResult]:
    """Union the runs, keep the closest copy of each id, return top-``k``.

    Unlike :func:`merge_results` this does not assume disjoint sources and is
    used when fusing overlapping result sets (e.g. an oversampled re-rank). Runs
    need not be pre-sorted.
    """
    best: dict[VectorId, SearchResult] = {}
    for run in runs:
        for r in run:
            cur = best.get(r.id)
            if cur is None or r.distance < cur.distance:
                best[r.id] = r
    ordered = sorted(best.values(), key=lambda r: (r.distance, r.id))
    return ordered[: max(0, k)]


__all__ = ["merge_dedup_keep_closest", "merge_results"]
