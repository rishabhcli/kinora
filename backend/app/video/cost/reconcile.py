"""Reconcile *estimated* vs *actual* cost and emit drift metrics.

A reservation is made at an estimate; a commit records what the provider actually
billed. The difference is *drift* — and a price sheet that systematically
under-estimates is dangerous, because the router would keep picking a provider on
a price it doesn't really charge and the hard cap would be reached sooner than the
estimator believes. So every commit feeds a :class:`DriftRecorder`, which keeps a
running, per-provider tally of signed drift (actual − estimated) plus aggregate
error metrics the FinOps layer / telemetry can chart and that a calibration step
could feed back into the sheets.

Pure logic, exact :class:`~app.video.cost.money.Money`, no clock or store; the
recorder is a deterministic accumulator just like
:class:`~app.providers.types.UsageTotals`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.video.cost.money import Currency, Money


@dataclass(frozen=True, slots=True)
class DriftSample:
    """One estimated-vs-actual observation for a single committed render."""

    provider: str
    model: str
    estimated: Money
    actual: Money
    shot_id: str | None = None

    def __post_init__(self) -> None:
        if self.estimated.currency is not self.actual.currency:
            from app.video.cost.money import CurrencyMismatch

            raise CurrencyMismatch(self.estimated.currency, self.actual.currency)

    @property
    def drift(self) -> Money:
        """Signed drift: actual − estimated (positive = under-estimated / over budget)."""
        return self.actual - self.estimated

    @property
    def relative_drift(self) -> Decimal:
        """Drift as a fraction of the estimate (``Decimal(0)`` when estimate is 0)."""
        if self.estimated.units == 0:
            return Decimal(0)
        return Decimal(self.drift.units) / Decimal(self.estimated.units)


@dataclass(frozen=True, slots=True)
class ProviderDrift:
    """Aggregate drift metrics for one provider."""

    provider: str
    samples: int
    estimated_total: Money
    actual_total: Money

    @property
    def total_drift(self) -> Money:
        return self.actual_total - self.estimated_total

    @property
    def mean_relative_drift(self) -> Decimal:
        """Total drift relative to total estimate (the calibration-relevant number)."""
        if self.estimated_total.units == 0:
            return Decimal(0)
        return Decimal(self.total_drift.units) / Decimal(self.estimated_total.units)

    @property
    def under_estimating(self) -> bool:
        """True when this provider costs *more* than the sheet predicts (risk side)."""
        return self.total_drift.units > 0


@dataclass
class DriftRecorder:
    """A deterministic accumulator of estimated-vs-actual drift, per provider.

    Single currency (the ledger's). ``record`` appends a :class:`DriftSample`;
    :meth:`by_provider` / :meth:`overall` summarize. The recorder never raises on
    drift — it measures it; acting on it (alerting, re-calibrating a sheet) is the
    caller's job.
    """

    currency: Currency = Currency.USD
    _estimated: dict[str, int] = field(default_factory=dict, repr=False)
    _actual: dict[str, int] = field(default_factory=dict, repr=False)
    _counts: dict[str, int] = field(default_factory=dict, repr=False)
    samples: list[DriftSample] = field(default_factory=list)

    def record(self, sample: DriftSample) -> DriftSample:
        if sample.actual.currency is not self.currency:
            from app.video.cost.money import CurrencyMismatch

            raise CurrencyMismatch(self.currency, sample.actual.currency)
        p = sample.provider.lower()
        self._estimated[p] = self._estimated.get(p, 0) + sample.estimated.units
        self._actual[p] = self._actual.get(p, 0) + sample.actual.units
        self._counts[p] = self._counts.get(p, 0) + 1
        self.samples.append(sample)
        return sample

    def record_estimate_actual(
        self,
        provider: str,
        model: str,
        estimated: Money,
        actual: Money,
        *,
        shot_id: str | None = None,
    ) -> DriftSample:
        """Convenience: build and record a :class:`DriftSample` in one call."""
        return self.record(
            DriftSample(
                provider=provider,
                model=model,
                estimated=estimated,
                actual=actual,
                shot_id=shot_id,
            )
        )

    def by_provider(self) -> dict[str, ProviderDrift]:
        return {
            p: ProviderDrift(
                provider=p,
                samples=self._counts[p],
                estimated_total=Money(self._estimated[p], self.currency),
                actual_total=Money(self._actual[p], self.currency),
            )
            for p in self._counts
        }

    def overall(self) -> ProviderDrift:
        """Aggregate drift across all providers (provider label ``"*"``)."""
        return ProviderDrift(
            provider="*",
            samples=sum(self._counts.values()),
            estimated_total=Money(sum(self._estimated.values()), self.currency),
            actual_total=Money(sum(self._actual.values()), self.currency),
        )

    def as_metrics(self) -> dict[str, object]:
        """Flat, structured-log/telemetry-friendly drift metrics."""
        overall = self.overall()
        out: dict[str, object] = {
            "samples": overall.samples,
            "estimated_total": str(overall.estimated_total.to_decimal()),
            "actual_total": str(overall.actual_total.to_decimal()),
            "total_drift": str(overall.total_drift.to_decimal()),
            "mean_relative_drift": str(overall.mean_relative_drift),
            "currency": self.currency.value,
        }
        per: dict[str, object] = {}
        for provider, drift in self.by_provider().items():
            per[provider] = {
                "samples": drift.samples,
                "total_drift": str(drift.total_drift.to_decimal()),
                "mean_relative_drift": str(drift.mean_relative_drift),
                "under_estimating": drift.under_estimating,
            }
        out["by_provider"] = per
        return out


__all__ = [
    "DriftRecorder",
    "DriftSample",
    "ProviderDrift",
]
