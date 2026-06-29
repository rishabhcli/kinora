"""Demand forecasting for the predictive autoscaler (kinora.md §4.5, §12.2).

A *reactive* autoscaler (scale when the queue is already deep) always lags by one
cold-start: by the time it sees the backlog and launches an instance, the reader
has waited the cold-start out (§instances). The fix is to scale on a **forecast**
of the next cold-start window's demand, not on the present. This module is the
forecaster: pure, online estimators over a stream of demand samples
(requests/second offered to a backend), each emitting a point forecast for a
lead-time horizon plus an uncertainty band the autoscaler turns into headroom.

Three estimators, increasing in power:

* :class:`EwmaForecaster` — exponentially-weighted level. Cheap, no trend; good for
  a stationary load. The forecast is flat at the current level.
* :class:`HoltForecaster` — double-exponential (Holt) level **+ trend**. Catches a
  ramp (the reader population growing through the evening) and projects it forward
  ``horizon`` steps. This is the workhorse.
* :class:`HoltWintersForecaster` — triple-exponential level + trend + **additive
  seasonality**. Catches the diurnal reading cycle (a daily peak) when the sample
  cadence covers a known period.

Every forecaster also tracks a residual standard deviation online (Welford), so a
caller can ask for a *quantile* forecast — ``level + z·σ`` — to size for the 95th
percentile of demand rather than the mean. That headroom is what keeps the buffer
above the §4.5 low watermark when demand is noisy, without permanently
over-provisioning.

All deterministic given the sample sequence; no clock, no RNG.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "Forecast",
    "Forecaster",
    "EwmaForecaster",
    "HoltForecaster",
    "HoltWintersForecaster",
    "z_for_quantile",
]


# Standard-normal quantiles for the common headroom targets (avoids a SciPy dep).
_Z_TABLE: dict[float, float] = {
    0.50: 0.0,
    0.75: 0.6745,
    0.80: 0.8416,
    0.90: 1.2816,
    0.95: 1.6449,
    0.975: 1.9600,
    0.99: 2.3263,
    0.995: 2.5758,
    0.999: 3.0902,
}


def z_for_quantile(q: float) -> float:
    """Standard-normal z for quantile ``q`` (rational approximation, no SciPy).

    Uses the project's table for the common targets and Acklam's inverse-normal
    approximation elsewhere — accurate to ~1e-4 across ``(0, 1)``, plenty for
    sizing headroom.
    """
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    if q in _Z_TABLE:
        return _Z_TABLE[q]
    if q < 0.5:
        return -z_for_quantile(1.0 - q)
    # Acklam's rational approximation for the upper tail.
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    p_low = 0.02425
    if q < p_low:  # pragma: no cover - q>=0.5 guaranteed by the recursion above
        r = math.sqrt(-2.0 * math.log(q))
        return (((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) / (
            (((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1.0
        )
    if q <= 1.0 - p_low:
        r = q - 0.5
        rr = r * r
        return (((((a[0] * rr + a[1]) * rr + a[2]) * rr + a[3]) * rr + a[4]) * rr + a[5]) * r / (
            ((((b[0] * rr + b[1]) * rr + b[2]) * rr + b[3]) * rr + b[4]) * rr + 1.0
        )
    r = math.sqrt(-2.0 * math.log(1.0 - q))
    return -(((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) / (
        (((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1.0
    )


@dataclass(frozen=True, slots=True)
class Forecast:
    """A point forecast for a future horizon plus an uncertainty band."""

    #: The expected demand at the horizon (mean, never negative).
    point: float
    #: Online residual standard deviation (forecast uncertainty).
    sigma: float
    #: How many steps ahead this forecast targets.
    horizon: int
    #: Number of samples the estimator has seen (a cold estimator is unreliable).
    samples: int

    def quantile(self, q: float) -> float:
        """The ``q``-quantile demand: ``point + z(q)·sigma``, floored at 0."""
        return max(0.0, self.point + z_for_quantile(q) * self.sigma)

    @property
    def is_warm(self) -> bool:
        """True once the estimator has enough samples to trust (≥ 3)."""
        return self.samples >= 3

    def to_dict(self) -> dict[str, float | int | bool]:
        """JSON projection for the capacity report."""
        return {
            "point": round(self.point, 5),
            "sigma": round(self.sigma, 5),
            "horizon": self.horizon,
            "samples": self.samples,
            "is_warm": self.is_warm,
        }


class Forecaster(Protocol):
    """An online demand forecaster: feed samples, ask for a horizon forecast."""

    def observe(self, value: float) -> None:
        """Ingest the next demand sample (requests/second)."""
        ...

    def forecast(self, horizon: int) -> Forecast:
        """Project demand ``horizon`` steps ahead from the current state."""
        ...


@dataclass
class _Residuals:
    """Online residual variance via Welford's algorithm (numerically stable)."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def push(self, residual: float) -> None:
        self.n += 1
        delta = residual - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (residual - self.mean)

    @property
    def sigma(self) -> float:
        if self.n < 2:
            return 0.0
        return math.sqrt(self.m2 / (self.n - 1))


@dataclass
class EwmaForecaster:
    """Exponentially-weighted level (no trend). Flat forecast at the current level.

    ``alpha`` is the smoothing factor in ``(0, 1]``: higher reacts faster, lower is
    smoother. Good for stationary demand.
    """

    alpha: float = 0.3
    level: float = 0.0
    _samples: int = 0
    _residuals: _Residuals = field(default_factory=_Residuals)

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")

    def observe(self, value: float) -> None:
        if self._samples == 0:
            self.level = value
        else:
            prediction = self.level
            self._residuals.push(value - prediction)
            self.level = self.alpha * value + (1.0 - self.alpha) * self.level
        self._samples += 1

    def forecast(self, horizon: int) -> Forecast:
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        return Forecast(
            point=max(0.0, self.level),
            sigma=self._residuals.sigma,
            horizon=horizon,
            samples=self._samples,
        )


@dataclass
class HoltForecaster:
    """Double-exponential (Holt) level + trend. Projects a linear ramp forward.

    ``alpha`` smooths the level, ``beta`` the trend. The ``horizon``-step forecast
    is ``level + horizon·trend`` — so a rising demand ramp is anticipated, not
    chased. This is the autoscaler's default forecaster.
    """

    alpha: float = 0.3
    beta: float = 0.1
    level: float = 0.0
    trend: float = 0.0
    _samples: int = 0
    _residuals: _Residuals = field(default_factory=_Residuals)

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        if not 0.0 <= self.beta <= 1.0:
            raise ValueError("beta must be in [0, 1]")

    def observe(self, value: float) -> None:
        if self._samples == 0:
            self.level = value
            self.trend = 0.0
        elif self._samples == 1:
            self.trend = value - self.level
            self.level = value
        else:
            prediction = self.level + self.trend
            self._residuals.push(value - prediction)
            prev_level = self.level
            self.level = self.alpha * value + (1.0 - self.alpha) * (self.level + self.trend)
            self.trend = self.beta * (self.level - prev_level) + (1.0 - self.beta) * self.trend
        self._samples += 1

    def forecast(self, horizon: int) -> Forecast:
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        # Residual sigma is for the 1-step error; widen by sqrt(horizon) for the
        # h-step band (random-walk error accumulation — a standard heuristic).
        sigma = self._residuals.sigma * math.sqrt(horizon)
        return Forecast(
            point=max(0.0, self.level + horizon * self.trend),
            sigma=sigma,
            horizon=horizon,
            samples=self._samples,
        )


@dataclass
class HoltWintersForecaster:
    """Triple-exponential level + trend + additive seasonality (diurnal demand).

    ``period`` is the number of samples in one season (e.g. 24 if a sample is an
    hour and the cycle is daily). Seasonal indices are seeded flat and learned
    online with smoothing ``gamma``. The forecast adds the seasonal index of the
    target slot back onto the Holt projection, so a known daily peak is
    anticipated. Falls back to Holt behaviour before a full period is seen.
    """

    period: int
    alpha: float = 0.3
    beta: float = 0.05
    gamma: float = 0.1
    level: float = 0.0
    trend: float = 0.0
    seasonals: list[float] = field(default_factory=list)
    _samples: int = 0
    _residuals: _Residuals = field(default_factory=_Residuals)

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("period must be >= 2")
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        if not 0.0 <= self.beta <= 1.0:
            raise ValueError("beta must be in [0, 1]")
        if not 0.0 <= self.gamma <= 1.0:
            raise ValueError("gamma must be in [0, 1]")
        if not self.seasonals:
            self.seasonals = [0.0] * self.period

    def observe(self, value: float) -> None:
        season_idx = self._samples % self.period
        if self._samples == 0:
            self.level = value
            self.trend = 0.0
        else:
            prediction = self.level + self.trend + self.seasonals[season_idx]
            self._residuals.push(value - prediction)
            prev_level = self.level
            detrended = value - self.seasonals[season_idx]
            self.level = self.alpha * detrended + (1.0 - self.alpha) * (self.level + self.trend)
            self.trend = self.beta * (self.level - prev_level) + (1.0 - self.beta) * self.trend
            self.seasonals[season_idx] = (
                self.gamma * (value - self.level) + (1.0 - self.gamma) * self.seasonals[season_idx]
            )
        self._samples += 1

    def forecast(self, horizon: int) -> Forecast:
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        target_idx = (self._samples + horizon - 1) % self.period
        seasonal = self.seasonals[target_idx] if self._samples >= self.period else 0.0
        sigma = self._residuals.sigma * math.sqrt(horizon)
        return Forecast(
            point=max(0.0, self.level + horizon * self.trend + seasonal),
            sigma=sigma,
            horizon=horizon,
            samples=self._samples,
        )


def _seed_from(samples: Sequence[float], forecaster: Forecaster) -> Forecaster:
    """Replay ``samples`` through ``forecaster`` (test/seed helper)."""
    for s in samples:
        forecaster.observe(s)
    return forecaster
