"""Per-tenant / per-book fairness — one book can't starve others (kinora.md §12.2).

§12.2's per-session fairness keeps one *reader* from starving others; this module
adds the **per-tenant/per-book** layer: when many books share the render pool, the
slots a class is granted (by :mod:`app.qos.wfq`) are split *across books* so a
single book with a huge backlog can't monopolise its class's share. The split is
**max-min fair**: every book with backlog gets an equal base slice, and any book
that needs less than its slice frees the remainder for the still-needy (round by
round, work-conserving). Within a book the items are then ordered by EDF/age.

This is a pure allocator (no I/O); the :mod:`app.qos.scheduler` calls it once per
class per dispatch round, then pops that many items from each book's sub-queue.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.qos.model import QoSItem


def max_min_fair_shares(demands: dict[str, int], capacity: int) -> dict[str, int]:
    """Split ``capacity`` integer slots across ``demands`` max-min fairly.

    Each key (a tenant/book) wants ``demands[key]`` slots. Max-min fairness gives
    every still-unsatisfied key an equal slice each round; a key needing fewer than
    its slice is fully satisfied and its leftover is redistributed to the rest. The
    result is work-conserving (allocates ``min(capacity, sum(demands))``) and never
    grants a key more than it demanded.

    Determinism: keys are processed in sorted order so a fixed input always yields a
    fixed allocation (important for reproducible tests).
    """
    capacity = max(0, capacity)
    grant: dict[str, int] = dict.fromkeys(demands, 0)
    if capacity == 0:
        return grant
    remaining = {k: max(0, d) for k, d in demands.items() if d > 0}
    free = capacity
    while free > 0 and remaining:
        keys = sorted(remaining)
        n = len(keys)
        base = free // n
        if base == 0:
            # Fewer slots than needy keys: hand one each to the keys with the
            # largest demand (deterministic, sorted tie-break), then stop.
            for k in sorted(keys, key=lambda x: (-remaining[x], x))[:free]:
                grant[k] += 1
                remaining[k] -= 1
                if remaining[k] == 0:
                    del remaining[k]
            free = 0
            break
        granted_this_round = 0
        for k in keys:
            take = min(base, remaining[k])
            grant[k] += take
            remaining[k] -= take
            granted_this_round += take
            if remaining[k] == 0:
                del remaining[k]
        free -= granted_this_round
        if granted_this_round == 0:
            break
    return grant


def group_by_book(items: Iterable[QoSItem]) -> dict[str, list[QoSItem]]:
    """Bucket items by their fairness key (tenant, else book)."""
    buckets: dict[str, list[QoSItem]] = defaultdict(list)
    for item in items:
        buckets[item.fairness_key].append(item)
    return dict(buckets)


def fair_book_allocation(items: Iterable[QoSItem], slots: int) -> dict[str, int]:
    """Max-min fair split of ``slots`` across the books present in ``items``."""
    buckets = group_by_book(items)
    demands = {book: len(group) for book, group in buckets.items()}
    return max_min_fair_shares(demands, slots)


def starvation_free(allocation: dict[str, int], demands: dict[str, int]) -> bool:
    """True when every book with demand and a non-empty pool received at least one
    slot, *unless* capacity was too scarce to cover all books (the base==0 case).

    A cheap invariant the scheduler/tests assert: under adequate capacity no
    demanding book is left at zero.
    """
    needy = [k for k, d in demands.items() if d > 0]
    if not needy:
        return True
    if sum(allocation.values()) >= len(needy):
        return all(allocation.get(k, 0) >= 1 for k in needy)
    # Scarce capacity: at least the granted slots went to distinct needy books.
    return sum(1 for k in needy if allocation.get(k, 0) >= 1) == sum(allocation.values())


__all__ = [
    "fair_book_allocation",
    "group_by_book",
    "max_min_fair_shares",
    "starvation_free",
]
