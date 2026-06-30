"""Shared deterministic fixtures/helpers for the video-experiment tests.

No infra, no network, no real time: a :class:`FakeClock` provides a monotone
seconds source the runner consumes, and all synthetic outcomes are produced from
a seeded :class:`random.Random` so every test is exactly reproducible.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Protocol

import pytest

from app.video.experiments import (
    ACCEPT_RATE,
    COST_PER_SECOND,
    FAILURE_RATE,
    LATENCY_MS,
    QUALITY_SCORE,
    MetricDirection,
    MetricKind,
    RenderOutcome,
    VideoExperiment,
    VideoMetric,
    VideoVariant,
)


class FakeClock:
    """A monotone, fully-controlled seconds clock for the runner.

    ``__call__`` advances by ``step`` each tick so :meth:`ExperimentRunner.elapsed_s`
    is deterministic; :meth:`advance` jumps forward explicitly (e.g. to trip the
    max-duration stop) without consuming a tick.
    """

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def two_arm_experiment(
    *,
    key: str = "exp",
    salt: str = "exp-salt",
    primary_kind: MetricKind = MetricKind.PROPORTION,
    primary_key: str = ACCEPT_RATE,
    primary_direction: MetricDirection = MetricDirection.INCREASE,
    with_failure_guardrail: bool = True,
    guardrail_margin: float = 0.10,
    min_samples_per_arm: int = 30,
    max_duration_s: float = 1_000_000.0,
    control_weight: int = 5000,
    treat_weight: int = 5000,
) -> VideoExperiment:
    """A canonical control-vs-treatment video experiment."""
    metrics: list[VideoMetric] = [
        VideoMetric(primary_key, primary_kind, primary_direction),
    ]
    if with_failure_guardrail:
        metrics.append(
            VideoMetric(
                FAILURE_RATE,
                MetricKind.PROPORTION,
                MetricDirection.DECREASE,
                is_guardrail=True,
                guardrail_margin=guardrail_margin,
            )
        )
    return VideoExperiment(
        key=key,
        variants=(
            VideoVariant("control", "dashscope", "wan-old", control_weight, is_control=True),
            VideoVariant(
                "treat", "dashscope", "wan-new", treat_weight, spec={"resolution": "1080P"}
            ),
        ),
        salt=salt,
        metrics=tuple(metrics),
        min_samples_per_arm=min_samples_per_arm,
        max_duration_s=max_duration_s,
    )


def bernoulli_outcomes(
    variant_key: str,
    n: int,
    *,
    accept_p: float = 1.0,
    fail_p: float = 0.0,
    rng: random.Random,
) -> Iterator[RenderOutcome]:
    """``n`` synthetic outcomes with given accept/failure probabilities."""
    for _ in range(n):
        succeeded = rng.random() >= fail_p
        accepted = (rng.random() < accept_p) if succeeded else None
        yield RenderOutcome(variant_key, succeeded=succeeded, accepted=accepted)


class _Sink(Protocol):
    def record(self, outcome: RenderOutcome) -> None: ...


def feed(
    sink: _Sink,
    key: str,
    n: int,
    *,
    accept_p: float,
    fail_p: float,
    rng: random.Random,
) -> None:
    """Record ``n`` synthetic Bernoulli outcomes into a collector or runner."""
    for outcome in bernoulli_outcomes(key, n, accept_p=accept_p, fail_p=fail_p, rng=rng):
        sink.record(outcome)


@pytest.fixture
def rng() -> random.Random:
    """A seeded RNG (every test that wants randomness shares this seed)."""
    return random.Random(20240630)


__all__ = [
    "ACCEPT_RATE",
    "COST_PER_SECOND",
    "FAILURE_RATE",
    "LATENCY_MS",
    "QUALITY_SCORE",
    "FakeClock",
    "bernoulli_outcomes",
    "feed",
    "two_arm_experiment",
]
