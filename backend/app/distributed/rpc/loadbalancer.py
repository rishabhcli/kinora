"""Load-balancing policies — pick one endpoint instance from many.

Once a logical service has more than one instance (the moment it's split out and
scaled), every call needs a policy for *which* instance to hit. The policies here
are the ones that matter in practice, each pure and deterministic given its
inputs (a seeded RNG where randomness is involved), so they unit-test without a
network:

* **round-robin** — even spread, the sane default for homogeneous instances.
* **random** — stateless, good when callers are many and uncoordinated.
* **least-connections** — pick the instance with the fewest in-flight calls; the
  right choice when request cost varies (a long render vs a cheap lookup).
* **power-of-two-choices (P2C)** — sample two at random, pick the lighter; gets
  ~90% of least-connections' benefit with O(1) state and no herd effect. This is
  the modern default for large fleets.
* **consistent-hash** — route by a key (``session_id`` / ``shot_hash``) so the
  same key sticks to the same instance — essential for the §12.3 caches and the
  per-session affinity the Scheduler wants (a reader's shots warm one worker).

Unhealthy instances (per the discovery snapshot) are filtered out before the
policy runs; a policy that finds no healthy instance signals the caller to fail
the pick (which the client turns into ``UNAVAILABLE``).
"""

from __future__ import annotations

import enum
import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.distributed.rpc.registry import ServiceInstance


class LoadBalancePolicy(enum.Enum):
    """The available instance-selection strategies."""

    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    LEAST_CONNECTIONS = "least_connections"
    P2C = "power_of_two_choices"
    CONSISTENT_HASH = "consistent_hash"


@dataclass
class InFlightTracker:
    """Counts in-flight calls per instance id (for connection-aware policies).

    The client increments on dispatch and decrements on completion; the
    least-connections / P2C balancers read it. Pure integer bookkeeping —
    deterministic and cheap.
    """

    _counts: dict[str, int] = field(default_factory=dict)

    def inc(self, instance_id: str) -> None:
        """Record a call starting on ``instance_id``."""
        self._counts[instance_id] = self._counts.get(instance_id, 0) + 1

    def dec(self, instance_id: str) -> None:
        """Record a call finishing on ``instance_id`` (floored at zero)."""
        self._counts[instance_id] = max(0, self._counts.get(instance_id, 0) - 1)

    def get(self, instance_id: str) -> int:
        """Current in-flight count for an instance."""
        return self._counts.get(instance_id, 0)


@dataclass
class LoadBalancer:
    """Selects one :class:`ServiceInstance` per call under a chosen policy.

    State (round-robin cursor, in-flight counts) is per-balancer and per-service,
    so one instance of this class is held per logical service by the client. The
    RNG is seeded for determinism in tests; production seeds from entropy.
    """

    policy: LoadBalancePolicy = LoadBalancePolicy.P2C
    tracker: InFlightTracker = field(default_factory=InFlightTracker)
    seed: int | None = None
    _rng: random.Random = field(init=False, repr=False)
    _rr_cursor: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def pick(
        self,
        instances: Sequence[ServiceInstance],
        *,
        hash_key: str | None = None,
    ) -> ServiceInstance | None:
        """Pick one healthy instance, or ``None`` if none are available.

        ``hash_key`` is required by :attr:`CONSISTENT_HASH` and ignored otherwise;
        a missing key under consistent-hash falls back to round-robin so a caller
        without affinity still gets a deterministic, spread-out pick.
        """
        healthy = [i for i in instances if i.health]
        if not healthy:
            return None
        if len(healthy) == 1:
            return healthy[0]

        if self.policy is LoadBalancePolicy.ROUND_ROBIN:
            return self._round_robin(healthy)
        if self.policy is LoadBalancePolicy.RANDOM:
            return self._rng.choice(healthy)
        if self.policy is LoadBalancePolicy.LEAST_CONNECTIONS:
            return self._least_connections(healthy)
        if self.policy is LoadBalancePolicy.P2C:
            return self._p2c(healthy)
        if self.policy is LoadBalancePolicy.CONSISTENT_HASH:
            if hash_key is None:
                return self._round_robin(healthy)
            return self._consistent_hash(healthy, hash_key)
        return healthy[0]  # pragma: no cover - exhaustive above

    # -- strategy implementations ------------------------------------------ #

    def _round_robin(self, healthy: Sequence[ServiceInstance]) -> ServiceInstance:
        chosen = healthy[self._rr_cursor % len(healthy)]
        self._rr_cursor = (self._rr_cursor + 1) % max(1, len(healthy))
        return chosen

    def _least_connections(self, healthy: Sequence[ServiceInstance]) -> ServiceInstance:
        return min(healthy, key=lambda i: (self.tracker.get(i.instance_id), i.instance_id))

    def _p2c(self, healthy: Sequence[ServiceInstance]) -> ServiceInstance:
        a, b = self._rng.sample(list(healthy), 2)
        ca, cb = self.tracker.get(a.instance_id), self.tracker.get(b.instance_id)
        if ca == cb:
            return a if a.instance_id <= b.instance_id else b
        return a if ca < cb else b

    def _consistent_hash(
        self, healthy: Sequence[ServiceInstance], hash_key: str
    ) -> ServiceInstance:
        """Rendezvous (HRW) hashing: pick the instance with the top weighted hash.

        Rendezvous hashing gives consistent-hashing's stickiness (the same key →
        the same instance) while *minimising remaps* when the instance set changes
        — only the keys that mapped to a removed instance move. Deterministic: no
        RNG, no ring state.
        """

        def score(inst: ServiceInstance) -> int:
            digest = hashlib.sha256(f"{hash_key}\x00{inst.instance_id}".encode()).digest()
            return int.from_bytes(digest[:8], "big")

        return max(healthy, key=score)


__all__ = [
    "InFlightTracker",
    "LoadBalancePolicy",
    "LoadBalancer",
]
