"""A Prometheus :class:`MetricsSink` adapter for the streaming log.

Implements the two-method :class:`~app.streaming.log.metrics.MetricsSink` protocol
over ``prometheus_client`` Counters + Summaries, so handing a broker a
:class:`PrometheusMetrics` makes the log's produce/fetch/commit/rebalance/cleanup
activity scrapeable through the standard ``/metrics`` registry — no change to the
log core, which only knows the sink protocol.

``prometheus_client`` is imported lazily inside the constructor so the log package
stays importable (and the in-memory broker fully usable) on a machine without it;
constructing :class:`PrometheusMetrics` without the dependency raises a clear
error pointing at the ``otel``/metrics extra. Metric + label names are sanitised
to the Prometheus charset, and collectors are created lazily per metric name so
the same sink can carry counters with different label sets.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["PrometheusMetrics"]

_INVALID = re.compile(r"[^a-zA-Z0-9_]")
_PREFIX = "kinora_streaming_"


def _sanitize(name: str) -> str:
    """Coerce a metric/label name into the Prometheus identifier charset."""
    cleaned = _INVALID.sub("_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


class PrometheusMetrics:
    """Bridges the log's :class:`MetricsSink` onto a Prometheus registry."""

    def __init__(self, *, registry: Any | None = None, prefix: str = _PREFIX) -> None:
        try:
            from prometheus_client import CollectorRegistry, Counter, Summary
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "PrometheusMetrics requires prometheus_client "
                "(installed via the backend's base deps / the 'otel' extra)"
            ) from exc
        self._Counter = Counter
        self._Summary = Summary
        self._registry = registry if registry is not None else CollectorRegistry()
        self._prefix = prefix
        self._counters: dict[tuple[str, frozenset[str]], Any] = {}
        self._summaries: dict[tuple[str, frozenset[str]], Any] = {}

    @property
    def registry(self) -> Any:
        """The Prometheus ``CollectorRegistry`` to expose at ``/metrics``."""
        return self._registry

    def _counter(self, name: str, label_keys: frozenset[str]) -> Any:
        key = (name, label_keys)
        collector = self._counters.get(key)
        if collector is None:
            collector = self._Counter(
                f"{self._prefix}{_sanitize(name)}_total",
                f"Kinora streaming log counter: {name}",
                labelnames=sorted(label_keys),
                registry=self._registry,
            )
            self._counters[key] = collector
        return collector

    def _summary(self, name: str, label_keys: frozenset[str]) -> Any:
        key = (name, label_keys)
        collector = self._summaries.get(key)
        if collector is None:
            collector = self._Summary(
                f"{self._prefix}{_sanitize(name)}",
                f"Kinora streaming log summary: {name}",
                labelnames=sorted(label_keys),
                registry=self._registry,
            )
            self._summaries[key] = collector
        return collector

    @staticmethod
    def _labels(labels: dict[str, str]) -> dict[str, str]:
        return {_sanitize(k): v for k, v in labels.items()}

    def incr(self, name: str, value: int = 1, **labels: str) -> None:
        sanitized = self._labels(labels)
        collector = self._counter(name, frozenset(sanitized))
        (collector.labels(**sanitized) if sanitized else collector).inc(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        sanitized = self._labels(labels)
        collector = self._summary(name, frozenset(sanitized))
        (collector.labels(**sanitized) if sanitized else collector).observe(value)
