"""Unit tests for per-provider health: rolling success-rate, p50/p95 latency, the
error-class histogram, and the exponential-cooldown circuit breaker. No network,
deterministic clock — every transition is asserted exactly."""

from __future__ import annotations

import pytest

from app.providers.errors import (
    AuthenticationError,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)
from app.video.routing.health import (
    CircuitState,
    ErrorClass,
    HealthConfig,
    ProviderHealth,
    classify_error,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _health(**cfg: object) -> tuple[ProviderHealth, FakeClock]:
    clock = FakeClock()
    config = HealthConfig(**cfg)  # type: ignore[arg-type]
    return ProviderHealth(name="b", config=config, _clock=clock), clock


# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #


def test_classify_error_buckets() -> None:
    assert classify_error(ProviderTimeout("t")) is ErrorClass.TIMEOUT
    assert classify_error(RateLimited("r")) is ErrorClass.RATE_LIMITED
    assert classify_error(AuthenticationError("a")) is ErrorClass.AUTH
    assert classify_error(ProviderBadRequest("b")) is ErrorClass.BAD_REQUEST
    assert classify_error(TransientProviderError("5xx")) is ErrorClass.SERVER
    assert classify_error(ProviderError("x")) is ErrorClass.OTHER  # non-retryable base
    assert classify_error(ValueError("?")) is ErrorClass.OTHER


# --------------------------------------------------------------------------- #
# circuit breaker
# --------------------------------------------------------------------------- #


def test_breaker_trips_after_threshold() -> None:
    h, _ = _health(failure_threshold=3, base_cooldown_s=30.0)
    assert h.available() is True
    h.record_failure(ErrorClass.SERVER)
    h.record_failure(ErrorClass.SERVER)
    assert h.state is CircuitState.CLOSED  # below threshold
    h.record_failure(ErrorClass.SERVER)
    assert h.state is CircuitState.OPEN
    assert h.available() is False  # inside cooldown
    assert h.total_failures == 3
    assert h.error_counts() == {"server": 3}


def test_breaker_half_open_probe_recovers_and_resets_backoff() -> None:
    h, clock = _health(failure_threshold=1, base_cooldown_s=30.0)
    h.record_failure()
    assert h.state is CircuitState.OPEN
    assert h.trips == 1
    clock.now = 31.0
    assert h.available() is True
    assert h.state is CircuitState.HALF_OPEN
    h.record_success()
    assert h.state is CircuitState.CLOSED
    assert h.trips == 0
    assert h.current_cooldown_s == 0.0
    assert h.consecutive_failures == 0


def test_breaker_half_open_failure_reopens() -> None:
    h, clock = _health(failure_threshold=1, base_cooldown_s=10.0)
    h.record_failure()
    clock.now = 11.0
    assert h.available() is True  # half-open
    h.record_failure()  # probe failed
    assert h.state is CircuitState.OPEN


def test_breaker_cooldown_grows_exponentially_and_caps() -> None:
    h, clock = _health(
        failure_threshold=1, base_cooldown_s=10.0, cooldown_multiplier=2.0, max_cooldown_s=50.0
    )
    # trip 1 -> 10s
    h.record_failure()
    assert h.current_cooldown_s == 10.0
    clock.now += 10.0
    assert h.available() is True  # half-open
    # trip 2 -> 20s
    h.record_failure()
    assert h.current_cooldown_s == 20.0
    clock.now += 20.0
    assert h.available() is True
    # trip 3 -> 40s
    h.record_failure()
    assert h.current_cooldown_s == 40.0
    clock.now += 40.0
    assert h.available() is True
    # trip 4 -> would be 80s, capped at 50s
    h.record_failure()
    assert h.current_cooldown_s == 50.0


def test_success_resets_consecutive_failures() -> None:
    h, _ = _health(failure_threshold=3)
    h.record_failure()
    h.record_failure()
    h.record_success()
    assert h.consecutive_failures == 0
    h.record_failure()
    h.record_failure()
    assert h.state is CircuitState.CLOSED  # streak was broken; below threshold again


# --------------------------------------------------------------------------- #
# rolling telemetry
# --------------------------------------------------------------------------- #


def test_success_rate_empty_is_fully_healthy() -> None:
    h, _ = _health()
    assert h.success_rate() == 1.0


def test_success_rate_over_window() -> None:
    h, _ = _health(outcome_window=4, failure_threshold=99)
    h.record_success()
    h.record_failure()
    h.record_success()
    h.record_failure()
    assert h.success_rate() == pytest.approx(0.5)
    # window is bounded: a fifth outcome evicts the oldest.
    h.record_success()  # now [F, S, F, S] -> 0.5 still
    assert h.success_rate() == pytest.approx(0.5)


def test_latency_percentiles_nearest_rank() -> None:
    h, _ = _health(latency_window=10)
    for ms in (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0):
        h.record_success(latency_ms=ms)
    # nearest-rank p50 over 10 sorted values -> rank ceil(0.5*10)=5 -> 50ms
    assert h.p50_latency_ms() == pytest.approx(50.0)
    # p95 -> rank ceil(0.95*10)=10 -> 100ms
    assert h.p95_latency_ms() == pytest.approx(100.0)


def test_latency_percentiles_empty_is_zero() -> None:
    h, _ = _health()
    assert h.p50_latency_ms() == 0.0
    assert h.p95_latency_ms() == 0.0


def test_rejection_counter() -> None:
    h, _ = _health()
    h.record_rejection()
    h.record_rejection()
    assert h.total_rejections == 2


def test_snapshot_is_jsonable() -> None:
    h, _ = _health(failure_threshold=1)
    h.record_success(latency_ms=12.0)
    h.record_failure(ErrorClass.TIMEOUT)
    snap = h.snapshot().as_dict()
    assert snap["name"] == "b"
    assert snap["state"] == CircuitState.OPEN.value
    assert snap["errors"] == {"timeout": 1}
    assert isinstance(snap["success_rate"], float)


def test_health_config_validates() -> None:
    with pytest.raises(ValueError):
        HealthConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        HealthConfig(cooldown_multiplier=0.5)
    with pytest.raises(ValueError):
        HealthConfig(base_cooldown_s=100.0, max_cooldown_s=10.0)
