"""Typed read-side over the Prometheus registry (§12.5).

:mod:`app.observability.metrics` owns the *write* side — a private
:class:`~prometheus_client.CollectorRegistry` and one-liner emit helpers. This
module is the matching *read* side: a small, dependency-light snapshot reader so

* **tests** can assert "the provider error counter went up by one" without
  parsing the exposition text format, and
* the **observability plane** can compute derived SLIs (provider error-rate,
  cache hit-ratio, total video-seconds spent) for a compact JSON HUD that
  complements the full Prometheus scrape.

Reading is pure and deterministic: it walks ``registry.collect()`` once and
indexes the samples. No metric is mutated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry

from app.observability.metrics import registry as _default_registry


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    """The count + sum of one histogram series (enough for a mean / rate)."""

    count: float = 0.0
    sum: float = 0.0

    @property
    def mean(self) -> float:
        """Mean observed value (``0.0`` when no observations)."""
        return self.sum / self.count if self.count else 0.0


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """An immutable index of every sample in a registry at one instant.

    ``counters`` / ``gauges`` map a fully-qualified series key to its value;
    ``histograms`` map the base metric name (with labels) to a
    :class:`HistogramSnapshot`. The series key encodes labels as
    ``name{a="x",b="y"}`` (sorted) so a labelled lookup is stable.
    """

    counters: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, HistogramSnapshot] = field(default_factory=dict)

    def counter(self, name: str, **labels: str) -> float:
        """Value of a counter series (``0.0`` when absent)."""
        return self.counters.get(_series_key(name, labels), 0.0)

    def gauge(self, name: str, **labels: str) -> float:
        """Value of a gauge series (``0.0`` when absent)."""
        return self.gauges.get(_series_key(name, labels), 0.0)

    def histogram(self, name: str, **labels: str) -> HistogramSnapshot:
        """Count/sum of a histogram series (empty snapshot when absent)."""
        return self.histograms.get(_series_key(name, labels), HistogramSnapshot())

    # -- derived SLIs ------------------------------------------------------- #

    def provider_error_rate(self, *, model: str, op: str) -> float:
        """Fraction of provider calls for ``(model, op)`` that ultimately failed."""
        calls = self.counter("kinora_provider_calls_total", model=model, op=op)
        errors = self.counter("kinora_provider_errors_total", model=model, op=op)
        return errors / calls if calls else 0.0

    def cache_hit_ratio(self) -> float:
        """Shot-cache hit ratio across all shots (``0.0`` with no lookups)."""
        hits = self.counter("kinora_cache_hits_total")
        misses = self.counter("kinora_cache_misses_total")
        total = hits + misses
        return hits / total if total else 0.0

    def video_seconds_spent(self) -> float:
        """Total Wan video-seconds spent (the budget-critical resource)."""
        return self.counter("kinora_video_seconds_spent_total")


def _series_key(name: str, labels: dict[str, str]) -> str:
    """Build the stable ``name{k="v",…}`` key for a series (sorted labels)."""
    if not labels:
        return name
    inner = ",".join(f'{k}="{labels[k]}"' for k in sorted(labels))
    return f"{name}{{{inner}}}"


def _sample_key(name: str, labels: dict[str, str]) -> str:
    return _series_key(name, labels)


def snapshot(registry: CollectorRegistry | None = None) -> MetricsSnapshot:
    """Walk a registry once and return a typed :class:`MetricsSnapshot`.

    Defaults to the Kinora metrics registry. Histogram count/sum samples are
    folded into one :class:`HistogramSnapshot` per series; ``_bucket`` /
    ``_created`` samples are ignored (the count/sum are sufficient for the SLIs
    here and keep the snapshot small).
    """
    reg = registry if registry is not None else _default_registry
    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}
    hist_count: dict[str, float] = {}
    hist_sum: dict[str, float] = {}

    for family in reg.collect():
        family_type = family.type
        for sample in family.samples:
            sname = sample.name
            labels = {k: v for k, v in sample.labels.items() if k != "le"}
            if family_type == "counter":
                if sname.endswith("_total"):
                    counters[_sample_key(sname, labels)] = sample.value
            elif family_type == "gauge":
                gauges[_sample_key(sname, labels)] = sample.value
            elif family_type == "histogram":
                if sname.endswith("_count"):
                    base = sname[: -len("_count")]
                    hist_count[_sample_key(base, labels)] = sample.value
                elif sname.endswith("_sum"):
                    base = sname[: -len("_sum")]
                    hist_sum[_sample_key(base, labels)] = sample.value

    histograms: dict[str, HistogramSnapshot] = {}
    for key in set(hist_count) | set(hist_sum):
        histograms[key] = HistogramSnapshot(
            count=hist_count.get(key, 0.0), sum=hist_sum.get(key, 0.0)
        )

    return MetricsSnapshot(counters=counters, gauges=gauges, histograms=histograms)


__all__ = [
    "HistogramSnapshot",
    "MetricsSnapshot",
    "snapshot",
]
