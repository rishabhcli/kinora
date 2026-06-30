"""Work-stealing / rebalancing across render workers (kinora.md §12.2).

Static assignment drifts: a worker that drew a burst of slow shots stays saturated
while another sits idle, so throughput sags below what the fleet could deliver.
The :class:`Rebalancer` is the work-stealing controller — it spots an *idle*
worker and a *backed-up* one and produces a :class:`StealPlan`: a set of shots to
migrate from the loaded worker to the idle one.

It is a **pure planner** over a load snapshot: it decides *what should move*, never
performs I/O. The coordinator applies a plan by reassigning the named shots
(re-acquiring their leases against the new worker, which advances the fence and
fences the old worker out — the same exactly-once handoff as crash recovery). That
separation keeps the steal policy unit-testable: feed it loads, assert the plan.

Policy:

* a worker is *idle* when it has free slots **and** holds strictly fewer leases
  than the fleet's busiest worker by more than ``imbalance_threshold``;
* migrate from the most-loaded eligible donor that the idle worker is *capable*
  of serving (same lane/provider), newest-first so committed continuity shots are
  disturbed last;
* never migrate a committed shot unless explicitly allowed (``steal_committed``):
  committed shots are sticky for continuity and cheap to leave put;
* cap migrations per pass (``max_steals``) so rebalancing nudges, never thrashes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from app.orchestration.models import Lane, ShotLease, WorkerDescriptor

logger = structlog.get_logger("app.orchestration.rebalance")

__all__ = ["RebalanceConfig", "Migration", "StealPlan", "Rebalancer"]


@dataclass(frozen=True, slots=True)
class RebalanceConfig:
    """Knobs for the work-stealing planner."""

    #: Min lease-count gap between busiest and idlest before stealing kicks in.
    imbalance_threshold: int = 2
    #: Max shots migrated in one planning pass (anti-thrash).
    max_steals: int = 4
    #: Whether committed shots may be stolen (default: leave them put for continuity).
    steal_committed: bool = False

    def __post_init__(self) -> None:
        if self.imbalance_threshold < 1 or self.max_steals < 1:
            raise ValueError("imbalance_threshold and max_steals must be >= 1")


@dataclass(frozen=True, slots=True)
class Migration:
    """A single shot to move from ``from_worker`` to ``to_worker``."""

    shot_hash: str
    from_worker: str
    to_worker: str
    lane: Lane
    book_id: str


@dataclass(frozen=True, slots=True)
class StealPlan:
    """A batch of migrations the coordinator should apply this pass."""

    migrations: tuple[Migration, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.migrations

    def shot_hashes(self) -> list[str]:
        return [m.shot_hash for m in self.migrations]


class Rebalancer:
    """Plans work-stealing migrations from a load snapshot (pure)."""

    def __init__(self, config: RebalanceConfig | None = None) -> None:
        self._cfg = config or RebalanceConfig()

    def plan(
        self,
        workers: Sequence[WorkerDescriptor],
        leases: Sequence[ShotLease],
    ) -> StealPlan:
        """Plan migrations that move work off backed-up workers onto idle ones.

        ``workers`` are the live, assignable workers; ``leases`` is every live
        lease. Returns an empty plan when the fleet is already balanced (gap below
        the threshold) or when no idle worker can serve any donor's shots.
        """
        assignable = [w for w in workers if w.accepts_work()]
        if len(assignable) < 2:
            return StealPlan()

        by_worker: dict[str, list[ShotLease]] = {w.worker_id: [] for w in assignable}
        for lease in leases:
            if lease.worker_id in by_worker:
                by_worker[lease.worker_id].append(lease)

        caps = {w.worker_id: w for w in assignable}
        counts = {wid: len(ls) for wid, ls in by_worker.items()}
        busiest = max(counts.values())
        idlest = min(counts.values())
        if busiest - idlest < self._cfg.imbalance_threshold:
            return StealPlan()

        migrations: list[Migration] = []
        # Idle candidates: those with free slots, lightest first.
        idle_order = sorted(
            (w for w in assignable if counts[w.worker_id] < w.capabilities.max_concurrency),
            key=lambda w: (counts[w.worker_id], w.worker_id),
        )
        # Donors: heaviest first.
        donor_order = sorted(assignable, key=lambda w: (-counts[w.worker_id], w.worker_id))

        # Working copy of counts so a single pass keeps the fleet from over-shifting.
        live_counts = dict(counts)
        for idle in idle_order:
            for donor in donor_order:
                if donor.worker_id == idle.worker_id:
                    continue
                migrations.extend(
                    self._steal_from(
                        donor=donor,
                        idle=idle,
                        donor_leases=by_worker[donor.worker_id],
                        caps=caps,
                        live_counts=live_counts,
                        already=migrations,
                    )
                )
                if len(migrations) >= self._cfg.max_steals:
                    return self._finish(migrations)
        return self._finish(migrations)

    def _steal_from(
        self,
        *,
        donor: WorkerDescriptor,
        idle: WorkerDescriptor,
        donor_leases: list[ShotLease],
        caps: Mapping[str, WorkerDescriptor],
        live_counts: dict[str, int],
        already: Sequence[Migration],
    ) -> list[Migration]:
        """Pick movable shots from one donor for one idle worker."""
        moved: list[Migration] = []
        taken = {m.shot_hash for m in already}
        # Newest leases first so old/continuity shots are disturbed last.
        candidates = sorted(donor_leases, key=lambda lease: lease.granted_at_ms, reverse=True)
        for lease in candidates:
            if lease.shot_hash in taken:
                continue
            if lease.lane is Lane.COMMITTED and not self._cfg.steal_committed:
                continue
            # The idle worker must be capable of this shot's lane + provider.
            idle_caps = caps[idle.worker_id].capabilities
            if lease.lane not in idle_caps.lanes:
                continue
            if idle_caps.providers and lease.provider not in idle_caps.providers:
                continue
            # Stop once moving would invert the imbalance (donor would drop below
            # idle's new count) or the idle worker is full.
            if live_counts[idle.worker_id] >= idle_caps.max_concurrency:
                break
            if live_counts[donor.worker_id] - 1 < live_counts[idle.worker_id] + 1:
                break
            moved.append(
                Migration(
                    shot_hash=lease.shot_hash,
                    from_worker=donor.worker_id,
                    to_worker=idle.worker_id,
                    lane=lease.lane,
                    book_id=lease.book_id,
                )
            )
            live_counts[donor.worker_id] -= 1
            live_counts[idle.worker_id] += 1
            taken.add(lease.shot_hash)
            if len(already) + len(moved) >= self._cfg.max_steals:
                break
        return moved

    def _finish(self, migrations: list[Migration]) -> StealPlan:
        capped = tuple(migrations[: self._cfg.max_steals])
        if capped:
            logger.info("rebalance.plan", migrations=len(capped))
        return StealPlan(migrations=capped)
