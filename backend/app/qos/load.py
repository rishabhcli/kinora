"""A deterministic synthetic-load generator for the QoS fabric (tests + sims).

Builds :class:`~app.qos.model.QoSItem` streams from a seeded RNG so a whole
backpressure / fairness / starvation scenario is reproducible bit-for-bit with no
infra, no network, and no real reader. Used by the unit tests and available as a
tiny offline simulator for capacity planning.

It is intentionally pure data generation — it never touches Redis, the real queue,
or the live video gate, so it can never spend video-seconds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.qos.clock import VirtualClock
from app.qos.model import QoSClass, QoSItem


@dataclass(slots=True)
class LoadGen:
    """A seeded factory of synthetic QoS items over a virtual clock."""

    clock: VirtualClock
    seed: int = 1234
    _rng: random.Random = None  # type: ignore[assignment]
    _counter: int = 0

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:05d}"

    def item(
        self,
        *,
        qos_class: QoSClass,
        book_id: str = "book_a",
        session_id: str | None = None,
        tenant_id: str | None = None,
        eta_s: float | None = None,
        value: float | None = None,
        cost_s: float = 5.0,
        id: str | None = None,
    ) -> QoSItem:
        """One item enqueued *now* on the virtual clock.

        ``eta_s`` (reading-time to the shot) becomes an absolute ``deadline`` of
        ``now + eta_s``; ``None`` leaves it deadline-less (cold/plan work). ``value``
        defaults to a class-tiered baseline so committed > speculative > cold worth.
        """
        now = self.clock.now()
        if value is None:
            value = {
                QoSClass.COMMITTED: 10.0,
                QoSClass.SPECULATIVE: 4.0,
                QoSClass.COLD: 1.0,
            }[qos_class]
        deadline = None if eta_s is None else now + eta_s
        return QoSItem(
            id=id or self._next_id(qos_class.name.lower()),
            qos_class=qos_class,
            book_id=book_id,
            tenant_id=tenant_id,
            session_id=session_id,
            enqueued_at=now,
            deadline=deadline,
            eta_s=eta_s,
            value=value,
            cost_s=cost_s,
        )

    def burst(
        self,
        n: int,
        *,
        qos_class: QoSClass,
        book_id: str = "book_a",
        session_id: str | None = None,
        eta_low: float = 5.0,
        eta_high: float = 240.0,
    ) -> list[QoSItem]:
        """``n`` items of one class with randomised (seeded) deadlines."""
        out: list[QoSItem] = []
        for _ in range(n):
            eta = self._rng.uniform(eta_low, eta_high)
            out.append(
                self.item(
                    qos_class=qos_class, book_id=book_id, session_id=session_id, eta_s=eta
                )
            )
        return out


__all__ = ["LoadGen"]
