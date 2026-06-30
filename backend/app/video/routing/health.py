"""Per-provider health tracking + a circuit breaker with exponential cooldown.

This is the *observability and gating* layer of the v2 router. Each backend gets
one :class:`ProviderHealth` record that tracks:

* a **rolling success rate** over a bounded window of recent outcomes,
* **p50 / p95 latency** over a bounded window of recent successful renders,
* an **error-class histogram** (how many timeouts vs. rate-limits vs. bad
  requests vs. server faults this backend has produced), and
* a **circuit breaker** (``CLOSED`` → ``OPEN`` → ``HALF_OPEN``) whose ``OPEN``
  cooldown grows **exponentially** with each consecutive trip and resets once a
  half-open probe succeeds.

Everything here is *pure logic* driven by an injectable monotonic clock — no
wall-clock reads, no RNG — so the breaker transitions and percentile math are
exhaustively testable without sleeping. It deliberately mirrors the shape of the
round-1 :class:`app.providers.video_router.BackendHealth` (``available`` /
``record_success`` / ``record_failure``) so the v2 router stays a strict
superset, but adds the rolling-window telemetry the policies select on.

The :class:`LiveVideoDisabled` spend gate is **never** recorded here — it is a
deliberate gate, not a fault. The router classifies it before reaching this
layer (see :mod:`app.video.routing.router`).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
)

#: A monotonic clock source (seconds). Injectable so tests advance time exactly.
Clock = Callable[[], float]


class CircuitState(StrEnum):
    """The circuit state of one backend in the v2 router."""

    CLOSED = "closed"  # healthy; route freely
    OPEN = "open"  # tripped; skip until the (exponential) cooldown elapses
    HALF_OPEN = "half_open"  # cooldown elapsed; allow a single probe attempt


class ErrorClass(StrEnum):
    """Coarse fault taxonomy for the per-provider error histogram.

    The router maps a raised :class:`~app.providers.errors.ProviderError` onto one
    of these so health telemetry and policies can reason about *why* a backend is
    failing (e.g. "throttled" backends are temporarily cheap to skip, "bad_request"
    is a spec problem that will recur everywhere).
    """

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    SERVER = "server"  # 5xx / generic retryable transport fault
    OTHER = "other"


def classify_error(exc: BaseException) -> ErrorClass:
    """Bucket a provider exception into an :class:`ErrorClass` (pure)."""
    # Order matters: the more specific subclasses must be tested first.
    if isinstance(exc, ProviderTimeout):
        return ErrorClass.TIMEOUT
    if isinstance(exc, RateLimited):
        return ErrorClass.RATE_LIMITED
    if isinstance(exc, AuthenticationError):
        return ErrorClass.AUTH
    if isinstance(exc, ProviderBadRequest):
        return ErrorClass.BAD_REQUEST
    if isinstance(exc, ProviderError):
        return ErrorClass.SERVER if exc.retryable else ErrorClass.OTHER
    return ErrorClass.OTHER


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Tunables for :class:`ProviderHealth` (deterministic; no env reads)."""

    #: Consecutive failures that trip the breaker ``CLOSED`` → ``OPEN``.
    failure_threshold: int = 3
    #: Base ``OPEN`` cooldown (seconds) for the *first* trip.
    base_cooldown_s: float = 30.0
    #: Each consecutive trip multiplies the cooldown by this factor (exponential
    #: backoff): trip 1 → base, trip 2 → base·f, trip 3 → base·f² … capped.
    cooldown_multiplier: float = 2.0
    #: Hard ceiling on the cooldown so a chronically-bad backend still gets probed.
    max_cooldown_s: float = 600.0
    #: How many recent outcomes the rolling success-rate window keeps.
    outcome_window: int = 50
    #: How many recent success latencies the percentile window keeps.
    latency_window: int = 50

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.base_cooldown_s < 0:
            raise ValueError("base_cooldown_s must be >= 0")
        if self.cooldown_multiplier < 1.0:
            raise ValueError("cooldown_multiplier must be >= 1.0")
        if self.max_cooldown_s < self.base_cooldown_s:
            raise ValueError("max_cooldown_s must be >= base_cooldown_s")
        if self.outcome_window < 1 or self.latency_window < 1:
            raise ValueError("windows must be >= 1")


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty list (pure).

    ``q`` is a fraction in ``[0, 1]``. Uses the nearest-rank method (no
    interpolation) so results are stable and dependency-free.
    """
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    # Nearest-rank: rank = ceil(q * n), 1-indexed.
    n = len(sorted_values)
    rank = max(1, min(n, int(-(-q * n // 1))))  # ceil(q*n) without importing math
    return sorted_values[rank - 1]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """A JSON-friendly point-in-time view of one backend's health."""

    name: str
    state: CircuitState
    consecutive_failures: int
    total_successes: int
    total_failures: int
    total_rejections: int
    success_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    trips: int
    cooldown_s: float
    errors: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_rejections": self.total_rejections,
            "success_rate": round(self.success_rate, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "trips": self.trips,
            "cooldown_s": round(self.cooldown_s, 2),
            "errors": dict(self.errors),
        }


@dataclass
class ProviderHealth:
    """Rolling health + an exponential-cooldown circuit breaker for one backend.

    The breaker trips ``OPEN`` after ``failure_threshold`` consecutive failures.
    Each trip lengthens the next ``OPEN`` window geometrically (``base ·
    multiplier^(trips-1)``, capped at ``max_cooldown_s``). When the cooldown
    elapses, :meth:`available` flips the breaker to ``HALF_OPEN`` in place and
    lets exactly one probe through; that probe's outcome either closes the breaker
    (success → reset trip count) or re-opens it with a longer cooldown (failure).
    """

    name: str
    config: HealthConfig = field(default_factory=HealthConfig)
    _clock: Clock = field(default=time.monotonic, repr=False)

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_rejections: int = 0
    trips: int = 0
    opened_at: float = 0.0
    _current_cooldown_s: float = 0.0

    # Rolling windows (bounded deques, set in __post_init__ for the configured size).
    _outcomes: deque[bool] = field(default_factory=deque, repr=False)
    _latencies_ms: deque[float] = field(default_factory=deque, repr=False)
    _errors: dict[ErrorClass, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._outcomes = deque(maxlen=self.config.outcome_window)
        self._latencies_ms = deque(maxlen=self.config.latency_window)

    # -- breaker gating --------------------------------------------------- #

    def available(self) -> bool:
        """True when the breaker would let a call through right now.

        Transitions ``OPEN`` → ``HALF_OPEN`` in place once the (exponential)
        cooldown elapses, so callers see the probe window without a separate tick.
        """
        if self.state is CircuitState.OPEN:
            if self._clock() - self.opened_at >= self._current_cooldown_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_rejection(self) -> None:
        """Count a call the breaker refused (skipped without attempting)."""
        self.total_rejections += 1

    # -- outcome recording ------------------------------------------------ #

    def record_success(self, *, latency_ms: float | None = None) -> None:
        """Record a successful render; closes the breaker and resets backoff."""
        self.total_successes += 1
        self.consecutive_failures = 0
        self.state = CircuitState.CLOSED
        self.trips = 0
        self._current_cooldown_s = 0.0
        self._outcomes.append(True)
        if latency_ms is not None and latency_ms >= 0:
            self._latencies_ms.append(float(latency_ms))

    def record_failure(self, error_class: ErrorClass = ErrorClass.OTHER) -> None:
        """Record a failed render; may trip / re-open the breaker.

        A failure while ``HALF_OPEN`` (the probe failed) always re-opens; otherwise
        the breaker opens once ``consecutive_failures`` reaches the threshold. Each
        open lengthens the next cooldown geometrically.
        """
        self.total_failures += 1
        self.consecutive_failures += 1
        self._outcomes.append(False)
        self._errors[error_class] = self._errors.get(error_class, 0) + 1
        if self.state is CircuitState.HALF_OPEN or (
            self.consecutive_failures >= self.config.failure_threshold
        ):
            self._open()

    def _open(self) -> None:
        self.trips += 1
        self.state = CircuitState.OPEN
        self.opened_at = self._clock()
        self._current_cooldown_s = self._cooldown_for_trip(self.trips)

    def _cooldown_for_trip(self, trips: int) -> float:
        """Exponential cooldown for the ``trips``-th consecutive open, capped."""
        exponent = max(0, trips - 1)
        cooldown = self.config.base_cooldown_s * (self.config.cooldown_multiplier**exponent)
        return min(cooldown, self.config.max_cooldown_s)

    # -- telemetry -------------------------------------------------------- #

    @property
    def current_cooldown_s(self) -> float:
        """The cooldown currently applied to the ``OPEN`` window (0 when closed)."""
        return self._current_cooldown_s

    def success_rate(self) -> float:
        """Rolling success fraction over the recent-outcome window (1.0 if empty).

        An empty window is treated as fully healthy so a fresh backend is not
        penalized before it has done any work.
        """
        if not self._outcomes:
            return 1.0
        wins = sum(1 for ok in self._outcomes if ok)
        return wins / len(self._outcomes)

    def p50_latency_ms(self) -> float:
        return self._latency_percentile(0.5)

    def p95_latency_ms(self) -> float:
        return self._latency_percentile(0.95)

    def _latency_percentile(self, q: float) -> float:
        if not self._latencies_ms:
            return 0.0
        return _percentile(sorted(self._latencies_ms), q)

    def error_counts(self) -> dict[str, int]:
        """The error-class histogram as a plain str→int dict (telemetry-safe)."""
        return {cls.value: count for cls, count in self._errors.items()}

    def snapshot(self) -> HealthSnapshot:
        """A full point-in-time :class:`HealthSnapshot` for logs / debug routes."""
        return HealthSnapshot(
            name=self.name,
            state=self.state,
            consecutive_failures=self.consecutive_failures,
            total_successes=self.total_successes,
            total_failures=self.total_failures,
            total_rejections=self.total_rejections,
            success_rate=self.success_rate(),
            p50_latency_ms=self.p50_latency_ms(),
            p95_latency_ms=self.p95_latency_ms(),
            trips=self.trips,
            cooldown_s=self._current_cooldown_s,
            errors=self.error_counts(),
        )


__all__ = [
    "CircuitState",
    "Clock",
    "ErrorClass",
    "HealthConfig",
    "HealthSnapshot",
    "ProviderHealth",
    "classify_error",
]
