"""The QoS scheduler — the backpressure & priority fabric tying the policies together.

A pure, in-memory policy engine over an injectable backlog + :class:`~app.qos.clock.Clock`.
It is **not** the production Redis queue; it's the decision core that the Scheduler /
worker pool can consult (or that an adapter can drive the real queue from), and it's
fully unit-testable with a virtual clock + synthetic load.

Responsibilities, composed from the sibling modules:

* **Admission + backpressure** (:mod:`app.qos.admission`) — admit / defer / reject on
  enqueue, protecting committed latency and signalling speculation slow-down.
* **Aging** (:mod:`app.qos.aging`) — promote long-waiting cold/speculative work so it
  never starves.
* **WFQ + strict priority** (:mod:`app.qos.wfq`) — committed reservation then a
  weighted-fair split of the remaining slots, so cold keeps a guaranteed slice.
* **Deadline EDF** (:mod:`app.qos.deadline`) — within a class, urgent (soon-needed)
  shots jump ahead of far-off ones.
* **Per-book fairness** (:mod:`app.qos.fairness`) — a class's granted slots are split
  max-min fairly across books, so one book can't starve another.
* **Load shedding** (:mod:`app.qos.shedding`) — under overload, drop the
  least-valuable droppable work first (never committed).

Dispatch order produced by :meth:`dispatch`: a deterministic list of items to start
*now* given ``available_slots``, honouring all the above. The scheduler tracks queued
backlog; the caller marks items started/done to drive WFQ/fairness across rounds.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.qos import aging, deadline, fairness, shedding, wfq
from app.qos.admission import AdmissionVerdict
from app.qos.admission import decide as admission_decide
from app.qos.clock import Clock, WallClock
from app.qos.config import QoSConfig
from app.qos.model import QoSClass, QoSItem, SheddingReason

logger = get_logger("app.qos.scheduler")


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """The outcome of one :meth:`QoSScheduler.dispatch` round."""

    #: Items to start *now*, in serve order (highest QoS priority first).
    dispatched: list[QoSItem]
    #: Items load-shed this round (overload trimming), with reasons.
    shed: list[shedding.ShedVictim] = field(default_factory=list)
    #: Per-class slot grants the WFQ tier produced (telemetry).
    allocation: dict[QoSClass, int] = field(default_factory=dict)

    @property
    def started_ids(self) -> list[str]:
        return [it.id for it in self.dispatched]


class QoSScheduler:
    """An in-memory multi-class priority/backpressure scheduler (pure policy)."""

    def __init__(self, *, config: QoSConfig | None = None, clock: Clock | None = None) -> None:
        self._config = config or QoSConfig()
        self._clock = clock or WallClock()
        # The admitted backlog: queued, not-yet-dispatched items.
        self._backlog: dict[str, QoSItem] = {}
        # In-flight items (dispatched, not yet completed) for cap/fairness accounting.
        self._inflight: dict[str, QoSItem] = {}

    @property
    def config(self) -> QoSConfig:
        return self._config

    @property
    def depth(self) -> int:
        """Current queued (admitted, not dispatched) backlog size."""
        return len(self._backlog)

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    # -- accounting helpers -------------------------------------------------- #

    def _session_inflight(self, session_id: str | None) -> int:
        if session_id is None:
            return 0
        n = sum(1 for it in self._backlog.values() if it.session_id == session_id)
        n += sum(1 for it in self._inflight.values() if it.session_id == session_id)
        return n

    def _book_speculative_depth(self, book_id: str) -> int:
        return sum(
            1
            for it in self._backlog.values()
            if it.book_id == book_id and it.qos_class is QoSClass.SPECULATIVE
        )

    def _backlog_by_class(self) -> dict[QoSClass, list[QoSItem]]:
        groups: dict[QoSClass, list[QoSItem]] = defaultdict(list)
        for item in self._backlog.values():
            groups[item.qos_class].append(item)
        return groups

    # -- admission ----------------------------------------------------------- #

    def admit(self, item: QoSItem) -> AdmissionVerdict:
        """Run admission control and, if admitted, add ``item`` to the backlog.

        Returns the verdict. A deferred or rejected item is **not** enqueued; the
        caller decides whether to drop it or retry later (defer = slow speculation).
        """
        now = self._clock.now()
        verdict = admission_decide(
            item,
            now,
            total_depth=self.depth,
            session_inflight=self._session_inflight(item.session_id),
            book_speculative_depth=self._book_speculative_depth(item.book_id),
            config=self._config,
        )
        if verdict.admit:
            self._backlog[item.id] = item
        else:
            logger.info(
                "qos.admission",
                item_id=item.id,
                qos_class=item.qos_class.name,
                admit=verdict.admit,
                defer=verdict.defer,
                reason=verdict.reason.value,
                depth=self.depth,
            )
        return verdict

    def remove(self, item_id: str) -> QoSItem | None:
        """Drop a queued item (e.g. cancelled by a seek); returns it if present."""
        return self._backlog.pop(item_id, None)

    # -- load shedding ------------------------------------------------------- #

    def shed_overload(self, target_depth: int | None = None) -> list[shedding.ShedVictim]:
        """Trim the backlog down to ``target_depth`` by dropping least-valuable work."""
        now = self._clock.now()
        survivors, victims = shedding.shed(
            list(self._backlog.values()), now, config=self._config, target_depth=target_depth
        )
        if victims:
            self._backlog = {it.id: it for it in survivors}
            for v in victims:
                logger.info(
                    "qos.shed",
                    item_id=v.item.id,
                    qos_class=v.item.qos_class.name,
                    reason=v.reason.value,
                    value_density=round(v.item.value_density(), 4),
                )
        return victims

    # -- dispatch ------------------------------------------------------------ #

    def dispatch(self, *, available_slots: int | None = None) -> DispatchResult:
        """Choose which queued items to start now, honouring the whole QoS policy.

        Steps: (1) shed overload, (2) age the backlog, (3) WFQ-allocate slots across
        classes, (4) within each class split slots max-min fairly across books, (5)
        pop each book's items in EDF/age order. The committed reservation + WFQ
        weights guarantee committed always wins and cold never starves; EDF makes
        soon-needed shots jump the queue; per-book fairness stops one book hogging a
        class's share.
        """
        now = self._clock.now()
        slots = self._config.total_slots if available_slots is None else max(0, available_slots)

        shed_victims = self.shed_overload()

        backlog_items = list(self._backlog.values())
        aging.apply_aging(backlog_items, now, config=self._config)

        # Effective-class backlog counts (aging can promote an item into a higher
        # class's pool for the purpose of slot allocation + serve order).
        effective: dict[str, QoSClass] = {
            it.id: QoSClass(aging.effective_class_value(it, now, config=self._config))
            for it in backlog_items
        }
        backlog_counts: dict[QoSClass, int] = defaultdict(int)
        for cls in effective.values():
            backlog_counts[cls] += 1

        allocation = wfq.allocate_slots(
            available_slots=slots, backlog=dict(backlog_counts), config=self._config
        )

        dispatched: list[QoSItem] = []
        for cls in (QoSClass.COMMITTED, QoSClass.SPECULATIVE, QoSClass.COLD):
            grant = allocation.get(cls)
            if grant <= 0:
                continue
            pool = [it for it in backlog_items if effective[it.id] is cls]
            dispatched.extend(self._pop_class(pool, grant, now))

        for item in dispatched:
            self._backlog.pop(item.id, None)
            self._inflight[item.id] = item

        return DispatchResult(
            dispatched=dispatched, shed=shed_victims, allocation=dict(allocation.per_class)
        )

    def _pop_class(self, pool: list[QoSItem], grant: int, now: float) -> list[QoSItem]:
        """Pick ``grant`` items from one class: per-book fair, then EDF/age within book."""
        if grant <= 0 or not pool:
            return []
        book_grants = fairness.fair_book_allocation(pool, grant)
        buckets = fairness.group_by_book(pool)
        chosen: list[QoSItem] = []
        for book, items in buckets.items():
            take = book_grants.get(book, 0)
            if take <= 0:
                continue
            ordered = sorted(
                items,
                key=lambda it: deadline.edf_key(
                    it, now, horizon_s=self._config.deadline_urgency_horizon_s
                ),
            )
            chosen.extend(ordered[:take])
        # Final serve order across books: EDF/age so the most urgent starts first.
        chosen.sort(
            key=lambda it: deadline.edf_key(
                it, now, horizon_s=self._config.deadline_urgency_horizon_s
            )
        )
        return chosen

    # -- completion ---------------------------------------------------------- #

    def complete(self, item_id: str) -> QoSItem | None:
        """Mark a dispatched item finished, freeing its slot for the next round."""
        return self._inflight.pop(item_id, None)

    def snapshot(self) -> dict[str, object]:
        """A telemetry snapshot of the fabric's state (for logs / a debug endpoint)."""
        now = self._clock.now()
        by_class = self._backlog_by_class()
        return {
            "depth": self.depth,
            "inflight": self.inflight_count,
            "by_class": {c.name: len(by_class.get(c, [])) for c in QoSClass},
            "oldest_wait_s": round(
                max((it.wait_s(now) for it in self._backlog.values()), default=0.0), 3
            ),
            "starving": [
                it.id
                for it in self._backlog.values()
                if aging.is_starving(it, now, config=self._config)
            ],
            "slow_speculation": self.depth >= self._config.speculation_slowdown_depth,
        }


__all__ = ["DispatchResult", "QoSScheduler", "SheddingReason"]
