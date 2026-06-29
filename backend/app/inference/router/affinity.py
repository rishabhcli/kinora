"""KV-cache-affinity routing — route same-prefix requests to the same worker.

A continuous-batching engine that has already processed a shared prefix (a system
prompt, a tenant's canon slice, §8.4) keeps that prefix's KV in cache; a later
request that shares the prefix can *skip its prefill* and start decoding almost
immediately. Routing two such requests to the **same** worker therefore turns a
cold prefill into a cache hit — the §12.3 "keyframe / canon-embedding" caching
idea, applied to the serving engine's KV.

This module decides *which worker* a request should go to, balancing two pulls
that fight each other:

* **Affinity** — prefer the worker whose KV already holds the request's prefix
  (a warm hit). The warmth signal comes from a
  :class:`~app.inference.router.protocols.PrefixCacheOracle`; the bundled
  :class:`ResidencyOracle` reads the workers' own resident-prefix LRUs, and
  facet B can swap a smarter (radix-tree) oracle in.
* **Load balance** — never pile every same-prefix request onto one hot worker
  while others idle. Above a utilization ceiling the warm worker is skipped in
  favour of the least-loaded eligible one.

The result is a *score* per candidate worker; the router admits onto the best
scorer that can actually fit the request.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .errors import RouterConfigError
from .protocols import PrefixCacheOracle
from .request import InferenceRequest
from .worker import Worker

#: A resolver from a worker id to its live :class:`Worker` (or ``None``).
WorkerResolver = Callable[[str], "Worker | None"]


class ResidencyOracle:
    """Default :class:`PrefixCacheOracle`: exact resident-prefix membership.

    Returns ``1.0`` if the worker's KV LRU currently holds the prefix key, else
    ``0.0``. It needs the live worker set, so it is constructed with a callable
    that resolves a worker id to its :class:`~app.inference.router.worker.Worker`.
    """

    def __init__(self, resolve: WorkerResolver) -> None:
        self._resolve = resolve

    def warm_fraction(self, prefix_key: str | None, worker_id: str) -> float:
        if prefix_key is None:
            return 0.0
        worker = self._resolve(worker_id)
        if worker is None:
            return 0.0
        return 1.0 if worker.has_prefix(prefix_key) else 0.0


@dataclass(frozen=True, slots=True)
class AffinityConfig:
    """Tunables for affinity-vs-balance routing.

    Attributes:
        affinity_weight: How strongly a warm prefix biases selection. Higher =
            stickier; ``0`` makes routing pure load-balancing.
        load_ceiling: Utilization above which a worker is treated as "hot" and
            its affinity bonus is suppressed, so a warm-but-saturated worker
            yields to a cold-but-free one. ``1.0`` disables the ceiling.
        balance_weight: How strongly low utilization is preferred among
            otherwise-equal candidates (the load-balance pull).
    """

    affinity_weight: float = 1.0
    load_ceiling: float = 0.85
    balance_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.affinity_weight < 0 or self.balance_weight < 0:
            raise RouterConfigError("affinity/balance weights must be non-negative")
        if not 0.0 < self.load_ceiling <= 1.0:
            raise RouterConfigError("load_ceiling must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class WorkerScore:
    """A scored candidate worker for a request."""

    worker_id: str
    score: float
    warm_fraction: float
    utilization: float
    fits: bool


class AffinityRouter:
    """Scores + selects the best worker for a request (affinity + balance)."""

    def __init__(
        self,
        oracle: PrefixCacheOracle,
        config: AffinityConfig | None = None,
    ) -> None:
        self._oracle = oracle
        self.config = config or AffinityConfig()

    def score(self, request: InferenceRequest, worker: Worker) -> WorkerScore:
        """Score one worker for ``request`` (higher is better)."""
        warm = self._oracle.warm_fraction(request.prefix_key, worker.worker_id)
        util = worker.utilization
        fits = worker.can_admit(request)
        # Affinity bonus is suppressed once the worker crosses the load ceiling,
        # so a hot warm worker stops attracting still more same-prefix work.
        affinity_term = warm * self.config.affinity_weight
        if util >= self.config.load_ceiling:
            affinity_term *= max(0.0, (1.0 - util) / max(1e-9, 1.0 - self.config.load_ceiling))
        balance_term = (1.0 - util) * self.config.balance_weight
        return WorkerScore(
            worker_id=worker.worker_id,
            score=affinity_term + balance_term,
            warm_fraction=warm,
            utilization=util,
            fits=fits,
        )

    def rank(self, request: InferenceRequest, workers: Sequence[Worker]) -> list[WorkerScore]:
        """Score every worker, best first; ties break on lower utilization then id."""
        scored = [self.score(request, w) for w in workers]
        scored.sort(key=lambda s: (-s.score, s.utilization, s.worker_id))
        return scored

    def select(self, request: InferenceRequest, workers: Sequence[Worker]) -> Worker | None:
        """Pick the best *fitting* worker for ``request``; ``None`` if none fit.

        Only workers that can actually admit the request are eligible — affinity
        never overrides capacity. Among fitting workers, the highest score wins.
        """
        eligible = {w.worker_id: w for w in workers if w.can_admit(request)}
        if not eligible:
            return None
        for s in self.rank(request, list(eligible.values())):
            if s.worker_id in eligible:
                return eligible[s.worker_id]
        return None  # pragma: no cover - eligible is non-empty


__all__ = [
    "AffinityConfig",
    "AffinityRouter",
    "ResidencyOracle",
    "WorkerResolver",
    "WorkerScore",
]
