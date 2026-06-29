"""Buggify — FoundationDB's fault-injection idiom, adapted for Kinora.

FoundationDB peppers its production code with ``BUGGIFY`` blocks: branches that do
nothing in a real build but, *only inside the simulator*, occasionally fire to
inject a delay, a reorder, a crash. The genius is that the perturbation lives at
the exact place a real failure would manifest, and whether it fires is a pure
function of the run seed — so a "Buggify storm" deterministically explores the
interleavings a real outage would, and a failing seed replays them exactly.

This module is that mechanism, made explicit. The simulated seams (:mod:`network`,
:mod:`storage`, :mod:`redis_sim`, and the worker model in :mod:`system`) call into
a :class:`Buggify` instance at their decision points — "should this message drop?"
"should this read be stale?" "should this worker stall, and for how long?" — and
:class:`Buggify` answers by consulting the run's :class:`FaultProfile` and rolling
the run's :class:`~app.verification.simulation.core.Prng`.

Two design rules borrowed from FDB:

#. **Injection is centralised and counted.** Every fired fault is recorded in
   :class:`BuggifyLog`, so a run can report "this seed injected 3 net drops, 1
   partition, 2 lease expiries" — the adversity is observable, not invisible. A
   bug report that says only "seed 1234 fails" is far less useful than one that
   says "seed 1234 fails when a partition overlaps a lease expiry."
#. **Faults draw from a dedicated PRNG stream.** Buggify ``split()``\\ s its own
   stream off the root so adding or removing a fault decision in one seam does not
   shift the faults another seam sees — the schedule stays stable as the code
   evolves (the whole reason FDB can keep a regression seed alive for years).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from app.verification.simulation.core import Prng
from app.verification.simulation.faults import FaultKind, FaultProfile


@dataclass(slots=True)
class BuggifyEvent:
    """A single fired fault, timestamped on the virtual clock for the trace."""

    t_ms: int
    kind: FaultKind
    where: str
    magnitude_ms: int = 0
    detail: str = ""


@dataclass(slots=True)
class BuggifyLog:
    """The observable record of every injected fault on a run.

    This is what turns "seed 1234 fails" into an actionable report. The shrinker
    and the failing-seed dump both read it; a passing run's log is the proof that
    the adversity actually fired (a profile that injected nothing proves nothing).
    """

    events: list[BuggifyEvent] = field(default_factory=list)

    def record(
        self, t_ms: int, kind: FaultKind, where: str, *, magnitude_ms: int = 0, detail: str = ""
    ) -> None:
        """Append one fired fault to the trace."""
        self.events.append(
            BuggifyEvent(
                t_ms=t_ms, kind=kind, where=where, magnitude_ms=magnitude_ms, detail=detail
            )
        )

    def counts(self) -> dict[str, int]:
        """A ``{kind: n}`` histogram of fired faults (for the report header)."""
        return dict(Counter(e.kind.value for e in self.events))

    @property
    def total(self) -> int:
        """How many faults fired in total."""
        return len(self.events)

    def summary(self) -> str:
        """A compact one-liner: ``net_drop×3, net_partition×1, qa_reject×2``."""
        counts = self.counts()
        if not counts:
            return "no faults fired"
        return ", ".join(f"{k}×{n}" for k, n in sorted(counts.items()))


class Buggify:
    """The fault-injection gate: roll the seeded PRNG against a profile.

    One :class:`Buggify` is created per run with the run's :class:`FaultProfile`,
    a PRNG stream split off the root, and a clock callable so fired faults are
    timestamped. Seams ask it yes/no and how-long questions; it answers
    deterministically and logs every "yes".

    The clock callable is injected (not a :class:`SimClock` reference) so this
    module stays decoupled from the loop — it only needs "what time is it" for the
    trace, never to advance time.
    """

    __slots__ = ("_profile", "_prng", "_now_ms", "log", "enabled")

    def __init__(
        self,
        profile: FaultProfile,
        prng: Prng,
        now_ms: Callable[[], int],
        *,
        log: BuggifyLog | None = None,
    ) -> None:
        self._profile = profile
        self._prng = prng
        self._now_ms = now_ms
        self.log = log if log is not None else BuggifyLog()
        #: A master switch the runtime can flip off to run a clean control pass.
        self.enabled = True

    @property
    def profile(self) -> FaultProfile:
        """The bound fault profile (read-only)."""
        return self._profile

    def should(self, kind: FaultKind, where: str, *, detail: str = "") -> bool:
        """Roll for an instantaneous fault (a drop, an error, a crash).

        Returns ``True`` (and logs) with the kind's effective probability. The
        ``where`` string is the injection site (``"net.send"``, ``"redis.eval"``)
        and lands in the trace so a report can point at the exact seam.
        """
        if not self.enabled:
            return False
        p = self._profile.probability(kind)
        if p <= 0.0:
            return False
        if self._prng.chance(p):
            self.log.record(self._now_ms(), kind, where, detail=detail)
            return True
        return False

    def duration(self, kind: FaultKind, where: str, *, detail: str = "") -> int:
        """Roll for a duration-shaped fault; return the injected delay in ms.

        Returns ``0`` if the fault does not fire this time; otherwise a value
        drawn uniformly from the kind's ``[min_ms, max_ms]`` band, logged with its
        magnitude. Used for added latency, worker stalls, and clock jumps.
        """
        if not self.enabled:
            return 0
        weight = self._profile.weight(kind)
        if weight.probability <= 0.0:
            return 0
        if not self._prng.chance(weight.probability):
            return 0
        lo, hi = weight.min_ms, max(weight.min_ms, weight.max_ms)
        magnitude = self._prng.randint(lo, hi) if hi > lo else lo
        self.log.record(self._now_ms(), kind, where, magnitude_ms=magnitude, detail=detail)
        return magnitude

    def roll_choice(self, n: int) -> int:
        """A raw seeded draw in ``[0, n)`` for non-fault randomness in a seam.

        e.g. picking *which* of two in-flight messages to reorder. Drawing from
        the same Buggify stream keeps the perturbation seed-stable.
        """
        if n <= 1:
            return 0
        return self._prng.randint(0, n - 1)


__all__ = [
    "Buggify",
    "BuggifyEvent",
    "BuggifyLog",
]
