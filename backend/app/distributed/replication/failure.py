"""Partition detection and healing: a deterministic heartbeat failure detector.

Active-active replication must notice when a peer becomes unreachable (stop
routing reads/writes there, stop counting it toward a quorum) and must trigger a
catch-up *reconciliation* the moment it comes back — that post-heal sync is what
repairs the divergence a partition caused.

This module is the pure detection logic:

* :class:`FailureDetector` — per-peer heartbeat tracking with a configurable
  timeout. A peer is *suspected* when no heartbeat has arrived within the
  timeout of the current time; *alive* otherwise. Deterministic: it is driven by
  explicit ``heartbeat(peer, at_ms)`` and queried at an explicit ``now_ms``, so
  there is no wall-clock dependence.
* :class:`PhiDetector` — an accrual (phi) detector: instead of a hard boolean it
  reports a rising *suspicion level* from the inter-arrival history, so a caller
  can pick its own threshold. The classic adaptive failure detector, kept pure
  with an injected sample window.
* :class:`PartitionMonitor` — tracks the alive/suspected transitions across all
  peers and emits :class:`PartitionEvent` records (``PARTITIONED`` /
  ``HEALED``). A HEALED event is the signal the gossip layer uses to kick an
  anti-entropy round so the two sides reconverge.

No transport, no clock side effects — the simulator and tests feed it events.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from app.distributed.replication.clock import NodeId

#: Default heartbeat timeout (ms): miss this long and a peer is suspected.
DEFAULT_TIMEOUT_MS = 3_000


class PeerState(Enum):
    ALIVE = "alive"
    SUSPECTED = "suspected"


class FailureDetector:
    """Timeout-based per-peer liveness, driven by explicit heartbeats and time.

    ``heartbeat(peer, at_ms)`` records a sign of life; :meth:`state` /
    :meth:`is_alive` answer at a queried ``now_ms``. A peer never heard from is
    suspected. The detector is monotone within a query: the same inputs always
    yield the same verdict.
    """

    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._timeout_ms = timeout_ms
        self._last_seen: dict[NodeId, int] = {}

    def heartbeat(self, peer: NodeId, at_ms: int) -> None:
        prev = self._last_seen.get(peer)
        # Heartbeats can arrive out of order; keep the latest sign of life.
        if prev is None or at_ms > prev:
            self._last_seen[peer] = at_ms

    def last_seen(self, peer: NodeId) -> int | None:
        return self._last_seen.get(peer)

    def state(self, peer: NodeId, now_ms: int) -> PeerState:
        last = self._last_seen.get(peer)
        if last is None or now_ms - last > self._timeout_ms:
            return PeerState.SUSPECTED
        return PeerState.ALIVE

    def is_alive(self, peer: NodeId, now_ms: int) -> bool:
        return self.state(peer, now_ms) is PeerState.ALIVE

    def alive_peers(self, peers: Iterable[NodeId], now_ms: int) -> frozenset[NodeId]:
        return frozenset(p for p in peers if self.is_alive(p, now_ms))


class PhiDetector:
    """An accrual (phi) failure detector over heartbeat inter-arrival times.

    Tracks a sliding window of inter-arrival gaps per peer and reports
    ``phi = -log10(P(now - last_arrival))`` under a normal model of the gaps.
    Higher phi == more suspicious; a caller chooses a threshold (8 is a common
    default). Pure: no timers, all driven by recorded arrivals and a queried now.
    """

    def __init__(self, window: int = 100, min_std_ms: float = 50.0) -> None:
        self._window = window
        self._min_std_ms = min_std_ms
        self._arrivals: dict[NodeId, int] = {}
        self._gaps: dict[NodeId, deque[float]] = {}

    def heartbeat(self, peer: NodeId, at_ms: int) -> None:
        prev = self._arrivals.get(peer)
        if prev is not None and at_ms > prev:
            gaps = self._gaps.setdefault(peer, deque(maxlen=self._window))
            gaps.append(float(at_ms - prev))
        if prev is None or at_ms > prev:
            self._arrivals[peer] = at_ms

    def _mean_std(self, peer: NodeId) -> tuple[float, float]:
        gaps = self._gaps.get(peer)
        if not gaps:
            return DEFAULT_TIMEOUT_MS, self._min_std_ms
        mean = sum(gaps) / len(gaps)
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        std = max(math.sqrt(var), self._min_std_ms)
        return mean, std

    def phi(self, peer: NodeId, now_ms: int) -> float:
        """The suspicion level for ``peer`` at ``now_ms`` (0 == just heard from)."""
        last = self._arrivals.get(peer)
        if last is None:
            return math.inf  # never seen -> maximally suspect
        elapsed = now_ms - last
        if elapsed <= 0:
            return 0.0
        mean, std = self._mean_std(peer)
        # Normal CDF tail probability that the next beat is still coming.
        z = (elapsed - mean) / std
        # P(later) = 1 - CDF(z); phi = -log10(P).
        p_later = 0.5 * math.erfc(z / math.sqrt(2))
        p_later = max(p_later, 1e-12)
        return -math.log10(p_later)

    def is_available(self, peer: NodeId, now_ms: int, *, threshold: float = 8.0) -> bool:
        return self.phi(peer, now_ms) < threshold


class PartitionEventKind(Enum):
    PARTITIONED = "partitioned"
    HEALED = "healed"


@dataclass(frozen=True, slots=True)
class PartitionEvent:
    """A detected liveness transition for one peer."""

    peer: NodeId
    kind: PartitionEventKind
    at_ms: int


class PartitionMonitor:
    """Tracks alive/suspected transitions and emits partition / heal events.

    Wraps a :class:`FailureDetector`. Call :meth:`observe` with the peer set and
    current time on each tick; it diffs against the last verdict and returns the
    transitions. A ``HEALED`` event is the cue to run anti-entropy with that peer.
    """

    def __init__(
        self,
        peers: Iterable[NodeId],
        detector: FailureDetector | None = None,
    ) -> None:
        self._detector = detector or FailureDetector()
        self._peers = frozenset(peers)
        # Start everyone ALIVE so the first miss is a real PARTITIONED transition.
        self._state: dict[NodeId, PeerState] = dict.fromkeys(self._peers, PeerState.ALIVE)

    @property
    def detector(self) -> FailureDetector:
        return self._detector

    def heartbeat(self, peer: NodeId, at_ms: int) -> None:
        self._detector.heartbeat(peer, at_ms)

    def current_state(self, peer: NodeId) -> PeerState:
        return self._state.get(peer, PeerState.ALIVE)

    def healthy_peers(self) -> frozenset[NodeId]:
        return frozenset(p for p, s in self._state.items() if s is PeerState.ALIVE)

    def observe(self, now_ms: int) -> list[PartitionEvent]:
        """Diff current detector verdicts against last state; emit transitions."""
        events: list[PartitionEvent] = []
        for peer in sorted(self._peers):
            new_state = self._detector.state(peer, now_ms)
            old_state = self._state[peer]
            if new_state is old_state:
                continue
            self._state[peer] = new_state
            kind = (
                PartitionEventKind.PARTITIONED
                if new_state is PeerState.SUSPECTED
                else PartitionEventKind.HEALED
            )
            events.append(PartitionEvent(peer, kind, now_ms))
        return events
