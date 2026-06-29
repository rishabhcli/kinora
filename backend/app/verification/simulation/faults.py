"""The fault-injection grammar (kinora.md §12 — "video generation is async and
flaky, so the backend is built like it").

A simulation is only as valuable as the adversity it survives. This module is the
*vocabulary* of adversity: an enumerable, seedable description of everything that
can go wrong in Kinora's control plane, plus a :class:`FaultProfile` that says how
*often* each thing goes wrong on a given run.

The split is deliberate:

* :class:`FaultKind` — the **closed set** of injectable failures, each tied to a
  real failure mode the §12 design must tolerate (a DashScope timeout, a Redis
  blip, a lease that expired because a worker stalled, a network partition between
  the api and the broker, a clock that jumped).
* :class:`FaultProfile` — the **knobs**: per-kind probabilities and magnitude
  ranges. A profile is a pure dataclass, so it serialises into a failing-seed
  report and replays exactly. ``FaultProfile.calm()`` is the happy path (used to
  prove the loop is correct *before* you break it); ``FaultProfile.chaos()`` turns
  everything up for the seed sweep.
* :class:`FaultSchedule` — a profile *bound to a seed*, the unit a run executes
  and a shrinker minimises. Two schedules with the same ``(seed, profile)`` inject
  byte-identical faults.

Nothing here injects anything; it only describes *what could* and *how likely*.
The actual injection lives in the simulated seams (:mod:`network`, :mod:`storage`,
:mod:`redis_sim`) and is gated through :mod:`buggify`, which reads a profile and
rolls the dice from the run's :class:`~app.verification.simulation.core.Prng`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class FaultKind(StrEnum):
    """The closed set of injectable failures, each mapped to a §12 failure mode.

    Keep this enumerable and exhaustive: a sweep that randomises over *all* of
    these is the proof that the design's resilience claims (backoff, DLQ, lease
    recovery, cooperative cancel, graceful degradation) actually hold rather than
    being asserted on paper.
    """

    # --- network seam (§12.6: api ↔ broker ↔ providers over a real network) --- #
    #: A network message is delayed by an extra, randomly-drawn latency.
    NET_LATENCY = "net_latency"
    #: A network message is dropped entirely (sender must retry / time out).
    NET_DROP = "net_drop"
    #: Two messages in flight are delivered out of order (reorder window).
    NET_REORDER = "net_reorder"
    #: A bidirectional partition isolates a node for a span of virtual time.
    NET_PARTITION = "net_partition"

    # --- storage seam (§12.3 caching layers, §12.6 OSS object store + DB) ------ #
    #: An object-store / DB read or write fails transiently (provider raises).
    DISK_IO_ERROR = "disk_io_error"
    #: A write *appears* to fail to the caller but actually lands (the nastiest:
    #: it tempts a double-spend on retry — exactly what idempotency must absorb).
    DISK_WRITE_LOST_ACK = "disk_write_lost_ack"
    #: A read returns stale data (the cache served a value behind the source of
    #: truth) — stresses eventual-consistency invariants.
    DISK_STALE_READ = "disk_stale_read"
    #: Extra latency on a storage op (slow OSS / cold DB connection).
    DISK_SLOW = "disk_slow"

    # --- redis seam (§12.1 the render queue lives on the managed broker) ------- #
    #: A redis command raises a transient connection error.
    REDIS_ERROR = "redis_error"
    #: Extra latency on a redis command (broker under load).
    REDIS_SLOW = "redis_slow"
    #: Redis loses non-persisted state (a flush / failover) for volatile keys.
    REDIS_FLUSH = "redis_flush"

    # --- worker / render seam (§12.1 the per-shot lifecycle) ------------------- #
    #: A worker stalls long enough that its lease expires (reaper must recover).
    WORKER_STALL = "worker_stall"
    #: A worker crashes mid-render (process dies; in-flight job orphaned).
    WORKER_CRASH = "worker_crash"
    #: The provider (DashScope / Wan) returns a transient render error.
    PROVIDER_TRANSIENT = "provider_transient"
    #: The provider hard-fails every attempt (drives the job to the DLQ → ladder).
    PROVIDER_HARD_FAIL = "provider_hard_fail"
    #: QA rejects a clip (drives the §9.5 repair loop → retry → degrade).
    QA_REJECT = "qa_reject"

    # --- clock seam (the subtle one: time is the simulator's only clock) ------- #
    #: A monotonic-ish clock jumps forward (GC pause, VM freeze, NTP step).
    CLOCK_JUMP = "clock_jump"


@dataclass(frozen=True, slots=True)
class FaultWeight:
    """How often, and how big, one fault kind fires.

    ``probability`` is the per-decision-point chance the fault triggers when
    Buggify rolls for this kind. ``min_ms`` / ``max_ms`` bound the magnitude for
    duration-shaped faults (added latency, stall length, clock jump); they are
    ignored for instantaneous faults (a drop, an error). Keeping magnitude here —
    not at the call site — means a failing run's profile fully reproduces it.
    """

    probability: float = 0.0
    min_ms: int = 0
    max_ms: int = 0

    def with_probability(self, p: float) -> FaultWeight:
        """Return a copy with a clamped probability (for shrinking)."""
        return replace(self, probability=max(0.0, min(1.0, p)))


def _w(p: float, lo: int = 0, hi: int = 0) -> FaultWeight:
    return FaultWeight(probability=p, min_ms=lo, max_ms=hi)


@dataclass(frozen=True, slots=True)
class FaultProfile:
    """The knobs: per-kind weights plus a global intensity multiplier.

    A profile is the adversary's configuration. It is intentionally a flat,
    serialisable dataclass so a failing seed report can print it verbatim and a
    replay can reconstruct it exactly. The :meth:`weight` accessor applies the
    global :attr:`intensity` multiplier, which the shrinker turns down to find the
    gentlest profile that still reproduces a bug.
    """

    weights: dict[FaultKind, FaultWeight] = field(default_factory=dict)
    #: Global multiplier on every probability (1.0 = as configured). The shrinker
    #: lowers this to test whether a bug needs the full storm or just a breeze.
    intensity: float = 1.0
    label: str = "profile"

    def weight(self, kind: FaultKind) -> FaultWeight:
        """The effective weight for ``kind`` after applying global intensity."""
        base = self.weights.get(kind, FaultWeight())
        if self.intensity == 1.0:
            return base
        return base.with_probability(base.probability * self.intensity)

    def probability(self, kind: FaultKind) -> float:
        """Convenience: the effective trigger probability for ``kind``."""
        return self.weight(kind).probability

    def with_intensity(self, intensity: float) -> FaultProfile:
        """Return a copy at a different global intensity (shrinker lever)."""
        return replace(self, intensity=max(0.0, intensity))

    def disabling(self, kind: FaultKind) -> FaultProfile:
        """Return a copy with ``kind`` turned off (shrinker lever: drop a fault)."""
        new = dict(self.weights)
        new[kind] = FaultWeight()
        return replace(self, weights=new)

    def active_kinds(self) -> list[FaultKind]:
        """The fault kinds with a non-zero effective probability."""
        return [k for k in FaultKind if self.probability(k) > 0.0]

    # ----------------------------------------------------------------------- #
    # Canonical profiles
    # ----------------------------------------------------------------------- #

    @staticmethod
    def calm() -> FaultProfile:
        """No faults at all — proves the loop is correct before breaking it."""
        return FaultProfile(weights={}, label="calm")

    @staticmethod
    def nominal() -> FaultProfile:
        """Production-plausible flakiness: occasional transients, rare partitions.

        These rates mirror "a real network with a real cloud provider": most ops
        succeed, retries occasionally fire, the odd lease expires. A run under
        ``nominal`` that violates an invariant is a genuine product bug, not a
        contrived storm.
        """
        return FaultProfile(
            label="nominal",
            weights={
                FaultKind.NET_LATENCY: _w(0.20, 5, 250),
                FaultKind.NET_DROP: _w(0.02),
                FaultKind.NET_REORDER: _w(0.05),
                FaultKind.NET_PARTITION: _w(0.005, 200, 2_000),
                FaultKind.DISK_IO_ERROR: _w(0.02),
                FaultKind.DISK_WRITE_LOST_ACK: _w(0.005),
                FaultKind.DISK_STALE_READ: _w(0.01),
                FaultKind.DISK_SLOW: _w(0.10, 5, 150),
                FaultKind.REDIS_ERROR: _w(0.01),
                FaultKind.REDIS_SLOW: _w(0.08, 2, 80),
                FaultKind.REDIS_FLUSH: _w(0.001),
                FaultKind.WORKER_STALL: _w(0.02, 1_000, 30_000),
                FaultKind.WORKER_CRASH: _w(0.005),
                FaultKind.PROVIDER_TRANSIENT: _w(0.10),
                FaultKind.PROVIDER_HARD_FAIL: _w(0.01),
                FaultKind.QA_REJECT: _w(0.10),
                FaultKind.CLOCK_JUMP: _w(0.005, 50, 1_000),
            },
        )

    @staticmethod
    def chaos() -> FaultProfile:
        """Everything turned up — the seed-sweep adversary (kinora.md §12).

        ``chaos`` is intentionally *unrealistic*: a network that drops one message
        in eight, a provider that hard-fails one render in twenty, leases that
        expire constantly. The point is not "production looks like this" but "if a
        latent ordering bug exists, this is the regime that flushes it out fast,"
        FoundationDB-style. Invariants must still hold; only the *quality* metric
        (buffer health) is allowed to sag under a storm.
        """
        return FaultProfile(
            label="chaos",
            weights={
                FaultKind.NET_LATENCY: _w(0.40, 10, 1_000),
                FaultKind.NET_DROP: _w(0.12),
                FaultKind.NET_REORDER: _w(0.25),
                FaultKind.NET_PARTITION: _w(0.05, 500, 8_000),
                FaultKind.DISK_IO_ERROR: _w(0.12),
                FaultKind.DISK_WRITE_LOST_ACK: _w(0.05),
                FaultKind.DISK_STALE_READ: _w(0.08),
                FaultKind.DISK_SLOW: _w(0.30, 20, 600),
                FaultKind.REDIS_ERROR: _w(0.08),
                FaultKind.REDIS_SLOW: _w(0.25, 5, 300),
                FaultKind.REDIS_FLUSH: _w(0.01),
                FaultKind.WORKER_STALL: _w(0.10, 2_000, 60_000),
                FaultKind.WORKER_CRASH: _w(0.04),
                FaultKind.PROVIDER_TRANSIENT: _w(0.30),
                FaultKind.PROVIDER_HARD_FAIL: _w(0.05),
                FaultKind.QA_REJECT: _w(0.30),
                FaultKind.CLOCK_JUMP: _w(0.03, 100, 5_000),
            },
        )


@dataclass(frozen=True, slots=True)
class FaultSchedule:
    """A fault profile bound to a seed — the executable, shrinkable unit.

    A schedule *is* a reproducible adversary: given ``(seed, profile)``, the
    simulation injects byte-identical faults every run. The shrinker produces new
    schedules from a failing one — same seed, gentler profile — to find the
    minimal adversary that still triggers the bug. A schedule prints cleanly into
    a failing-seed report and reconstructs from it.
    """

    seed: int
    profile: FaultProfile

    def describe(self) -> str:
        """A one-line, copy-pasteable description for a failing-seed report."""
        kinds = ", ".join(
            f"{k.value}@{self.profile.probability(k):.3g}"
            for k in self.profile.active_kinds()
        )
        return (
            f"FaultSchedule(seed={self.seed}, profile={self.profile.label}, "
            f"intensity={self.profile.intensity:g}, active=[{kinds}])"
        )


__all__ = [
    "FaultKind",
    "FaultProfile",
    "FaultSchedule",
    "FaultWeight",
]
