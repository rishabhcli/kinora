"""Pure placement policy: which worker should render a shot (kinora.md §12.2).

Given a ticket and a snapshot of (assignable workers, their current load, provider
capacity, and the book→worker stickiness map), :func:`choose_worker` returns the
best worker — or ``None`` when no capable worker has room. It is a **pure
function**: no I/O, no clock, no store. The coordinator gathers the snapshot, calls
this, and applies the result; tests call it directly with hand-built snapshots.

The policy, in priority order:

1. **Capability** — the worker must be able to serve the ticket's lane + provider
   (otherwise it physically cannot render it). Hard filter.
2. **Provider capacity** — the ticket's provider must admit one more shot (slots
   free + video-seconds headroom, via the :class:`CapacityOracle`). Hard filter.
3. **Locality (sticky)** — strongly prefer the worker already rendering this book,
   so a book's shots stay on one worker for visual continuity (warm references,
   warm canon cache). A committed ticket weights locality even higher.
4. **Least-loaded** — among the remaining candidates, pick the one with the most
   free local slots, breaking ties by worker_id for determinism.

Returning ``None`` is meaningful: the coordinator leaves the ticket queued (it
will retry next tick when a slot frees), rather than forcing it onto an incapable
or saturated worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.orchestration.capacity import CapacityOracle
from app.orchestration.models import ShotTicket, WorkerDescriptor

__all__ = ["WorkerLoad", "PlacementScore", "choose_worker", "score_candidates"]


@dataclass(frozen=True, slots=True)
class WorkerLoad:
    """A worker's live load: leases held overall + on the ticket's book."""

    worker_id: str
    leases_held: int
    #: Of those, how many are for the *same book* as the ticket (locality signal).
    leases_for_book: int

    def free_slots(self, max_concurrency: int) -> int:
        return max(0, max_concurrency - self.leases_held)


@dataclass(frozen=True, slots=True)
class PlacementScore:
    """A scored candidate (higher ``score`` wins; ties break on ``worker_id``)."""

    worker_id: str
    score: float
    free_slots: int
    is_sticky: bool


def _is_capable(worker: WorkerDescriptor, ticket: ShotTicket) -> bool:
    return worker.accepts_work() and worker.capabilities.can_serve(ticket)


def score_candidates(
    ticket: ShotTicket,
    workers: Sequence[WorkerDescriptor],
    loads: Mapping[str, WorkerLoad],
    *,
    oracle: CapacityOracle,
    sticky_book_owner: str | None = None,
) -> list[PlacementScore]:
    """Score every *eligible* worker for ``ticket`` (capability + capacity passed).

    ``sticky_book_owner`` is the worker currently designated for this book (the
    locality anchor); a candidate matching it gets a large bonus so a book sticks
    to one worker unless that worker is full or gone.
    """
    capacity = oracle.capacity_for(ticket.provider)
    # Provider must admit one more shot of this size at all.
    if not capacity.admits(video_seconds=ticket.video_seconds):
        return []

    scored: list[PlacementScore] = []
    for worker in workers:
        if not _is_capable(worker, ticket):
            continue
        load = loads.get(
            worker.worker_id, WorkerLoad(worker.worker_id, leases_held=0, leases_for_book=0)
        )
        free = load.free_slots(worker.capabilities.max_concurrency)
        if free <= 0:
            continue  # worker is at its local concurrency cap

        is_sticky = worker.worker_id == sticky_book_owner
        # Base score: free capacity (least-loaded preference).
        score = float(free)
        # Locality: a worker already on this book is strongly preferred; the
        # bonus dominates the free-slot term so stickiness wins among workers that
        # both have room. Committed shots (continuity-critical) weight it more.
        if is_sticky:
            score += 1000.0 if ticket.is_committed else 500.0
        elif load.leases_for_book > 0:
            # Even without the explicit anchor, having sibling shots of this book
            # is a soft locality signal.
            score += 100.0 + load.leases_for_book
        scored.append(
            PlacementScore(
                worker_id=worker.worker_id, score=score, free_slots=free, is_sticky=is_sticky
            )
        )
    return scored


def choose_worker(
    ticket: ShotTicket,
    workers: Sequence[WorkerDescriptor],
    loads: Mapping[str, WorkerLoad],
    *,
    oracle: CapacityOracle,
    sticky_book_owner: str | None = None,
) -> str | None:
    """The best worker_id for ``ticket``, or ``None`` if none is eligible."""
    scored = score_candidates(
        ticket, workers, loads, oracle=oracle, sticky_book_owner=sticky_book_owner
    )
    if not scored:
        return None
    # Highest score wins; deterministic tie-break on worker_id.
    best = max(scored, key=lambda s: (s.score, _neg_lex(s.worker_id)))
    return best.worker_id


def _neg_lex(worker_id: str) -> tuple[int, ...]:
    """Key that makes ``max`` prefer the lexicographically *smallest* worker_id."""
    return tuple(-ord(ch) for ch in worker_id)
