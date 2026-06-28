"""SLO evaluation + breach detection driving auto-rollback (kinora.md §12.5).

During the VERIFYING phase of a rollout the orchestrator watches a stream of
metric samples coming off the new version (the canary fleet, or the freshly
promoted blue/green slot). The signals are the rollout-gating subset of §12.5:
render success ratio, p99 render latency, error rate, queue-depth growth,
accepted-footage efficiency (§13). If any :class:`~deploy.orchestrator.models.SLOTarget`
breaches beyond its tolerance, verification fails and the orchestrator rolls
back automatically.

A metric *source* is a :class:`MetricSource` Protocol returning one sample
(a ``{name: value}`` map) per call. Production wires a source that scrapes the
``/metrics`` Prometheus endpoint of the new version; tests inject a scripted
list of samples. Pure decision logic, no scraping here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from deploy.orchestrator.models import SLOResult, SLOTarget


@runtime_checkable
class MetricSource(Protocol):
    """Yields one metric sample (a name→value map) per ``read()`` call."""

    async def read(self) -> Mapping[str, float]:
        """Return the latest scrape of the gating metrics."""
        ...


class SLOEvaluator:
    """Folds a stream of metric samples against a set of SLO targets.

    Stateful across samples: it tracks, per target, the worst observed value and
    the current run of consecutive breaching samples (so ``breach_tolerance > 1``
    requires that many *in a row*, not merely that many total). A target is
    "breached" once its consecutive-breach run reaches its tolerance.
    """

    __slots__ = ("_targets", "_worst", "_consecutive", "_max_consecutive", "_count")

    def __init__(self, targets: Sequence[SLOTarget]) -> None:
        if not targets:
            raise ValueError("SLOEvaluator requires at least one target")
        names = [t.name for t in targets]
        if len(names) != len(set(names)):
            raise ValueError("SLO target names must be unique")
        self._targets = tuple(targets)
        self._worst: dict[str, float] = {}
        self._consecutive: dict[str, int] = defaultdict(int)
        self._max_consecutive: dict[str, int] = defaultdict(int)
        self._count = 0

    @property
    def targets(self) -> tuple[SLOTarget, ...]:
        return self._targets

    @property
    def samples_seen(self) -> int:
        return self._count

    def observe(self, sample: Mapping[str, float]) -> None:
        """Fold one metric sample into the running evaluation."""
        self._count += 1
        for target in self._targets:
            if target.name not in sample:
                # A missing metric does not count as a breach (treat as absent),
                # but it does reset the consecutive run so a gap can't accumulate.
                self._consecutive[target.name] = 0
                continue
            value = float(sample[target.name])
            self._track_worst(target, value)
            if target.is_breaching(value):
                self._consecutive[target.name] += 1
                self._max_consecutive[target.name] = max(
                    self._max_consecutive[target.name], self._consecutive[target.name]
                )
            else:
                self._consecutive[target.name] = 0

    def _track_worst(self, target: SLOTarget, value: float) -> None:
        prev = self._worst.get(target.name)
        if prev is None:
            self._worst[target.name] = value
        elif target.higher_is_better:
            self._worst[target.name] = min(prev, value)  # lowest is worst
        else:
            self._worst[target.name] = max(prev, value)  # highest is worst

    def result_for(self, target: SLOTarget) -> SLOResult:
        consecutive = self._max_consecutive.get(target.name, 0)
        worst = self._worst.get(target.name, float("nan"))
        breached = consecutive >= target.breach_tolerance
        return SLOResult(
            target=target,
            breached=breached,
            worst_value=worst,
            samples=self._count,
            consecutive_breaches=consecutive,
        )

    def results(self) -> list[SLOResult]:
        return [self.result_for(t) for t in self._targets]

    @property
    def breached(self) -> bool:
        """True iff *any* target is currently in breach (tolerance reached)."""
        return any(self.result_for(t).breached for t in self._targets)

    def breaches(self) -> list[SLOResult]:
        return [r for r in self.results() if r.breached]

    def reset(self) -> None:
        self._worst.clear()
        self._consecutive.clear()
        self._max_consecutive.clear()
        self._count = 0


#: A sensible default rollout-gating SLO set for the Kinora render fleet (§12.5,
#: §13). Tuned to the off-gate reality: with ``KINORA_LIVE_VIDEO`` off the
#: render-worker still drains jobs (Ken-Burns), so success ratio and queue
#: health are the meaningful gates, while video-spend metrics stay 0.
DEFAULT_RENDER_SLOS: tuple[SLOTarget, ...] = (
    SLOTarget(name="render_success_ratio", threshold=0.95, higher_is_better=True),
    SLOTarget(name="error_rate", threshold=0.05, higher_is_better=False),
    SLOTarget(
        name="render_p99_latency_ms",
        threshold=120_000.0,
        higher_is_better=False,
        breach_tolerance=2,
        unit="ms",
    ),
    SLOTarget(
        name="queue_depth_growth",
        threshold=0.0,
        higher_is_better=False,
        breach_tolerance=3,
    ),
)
