"""A catalogue of named Kinora game-day scenarios (kinora.md §4.11 / §12.1).

These are ready-made :class:`~app.chaos.experiment.ChaosExperiment` builders for
the failure modes the design says the system must absorb: the provider
rate-limit storm, a Redis outage stranding the queue, Postgres latency, partial
object-store reads of expiring video URLs, and a clock-skew that threatens TTLs.
Each is a pure constructor — no I/O — so the runner (with a real or fake probe)
drives them, and tests assert the schedule/hypothesis without a live system.

Dependency names are the Kinora seams chaos scopes to: ``"dashscope"`` (model
provider), ``"redis"`` (queue + pub/sub), ``"postgres"`` (canon/state),
``"object_store"`` (MinIO/S3 media). A scenario's blast radius is exactly the
dependencies it touches, so e.g. the provider-storm scenario can never perturb
Redis or Postgres.
"""

from __future__ import annotations

from app.chaos.experiment import AbortConditions, ChaosExperiment, ScheduledFault
from app.chaos.faults import (
    ClockSkewFault,
    ConnectionDropFault,
    DependencyDownFault,
    LatencyFault,
    PartialResponseFault,
    RateLimitStormFault,
)
from app.chaos.steady_state import (
    SteadyStateHypothesis,
    availability_at_least,
    error_rate_at_most,
    latency_at_most,
)

# Canonical dependency-seam names chaos scopes to.
DASHSCOPE = "dashscope"
REDIS = "redis"
POSTGRES = "postgres"
OBJECT_STORE = "object_store"


def provider_rate_limit_storm(seed: int = 1337) -> ChaosExperiment:
    """DashScope throttles after a few calls — does ingest degrade, not die?

    Hypothesis: even while the image/video provider 429s, the reader keeps a high
    availability (the pipeline falls back to the Ken-Burns degradation lane) and
    the error rate stays bounded.
    """
    return ChaosExperiment.of(
        name="provider_rate_limit_storm",
        description="DashScope 429 Throttling.RateQuota storm on the image model.",
        hypothesis=SteadyStateHypothesis.of(
            [availability_at_least(0.95), error_rate_at_most(0.05)],
            description="reader stays available via the degradation ladder",
        ),
        blast_radius=[DASHSCOPE],
        schedule=[
            ScheduledFault(
                RateLimitStormFault(dependency=DASHSCOPE, name="dashscope_429", allow_first=3),
                arm_at_s=2.0,
                hold_s=20.0,
            )
        ],
        duration_s=30.0,
        poll_interval_s=2.0,
        abort=AbortConditions(max_injected_errors=200, breach_tolerance=2),
        seed=seed,
    )


def redis_outage(seed: int = 1337) -> ChaosExperiment:
    """Redis goes hard-down — does the queue survive and recover (§12.1)?

    Hypothesis: availability dips but stays above the floor; the render queue and
    pub/sub absorb the outage without a cascading error storm.
    """
    return ChaosExperiment.of(
        name="redis_outage",
        description="Redis (queue + pub/sub) unavailable for a window.",
        hypothesis=SteadyStateHypothesis.of(
            [availability_at_least(0.90), error_rate_at_most(0.10)],
        ),
        blast_radius=[REDIS],
        schedule=[
            ScheduledFault(
                DependencyDownFault(dependency=REDIS, name="redis_down"),
                arm_at_s=3.0,
                hold_s=10.0,
            )
        ],
        duration_s=25.0,
        poll_interval_s=1.0,
        abort=AbortConditions(breach_tolerance=1),
        seed=seed,
    )


def postgres_latency_brownout(seed: int = 1337) -> ChaosExperiment:
    """Postgres gets slow (not down) — does p99 stay under the SLO?

    Hypothesis: injected DB latency raises p99 but it stays under the ceiling and
    availability is unaffected (no errors, just slower).
    """
    return ChaosExperiment.of(
        name="postgres_latency_brownout",
        description="Postgres adds 0.5s+jitter per call for a window.",
        hypothesis=SteadyStateHypothesis.of(
            [latency_at_most(1500.0, metric="p99_latency_ms"), availability_at_least(0.99)],
        ),
        blast_radius=[POSTGRES],
        schedule=[
            ScheduledFault(
                LatencyFault(
                    dependency=POSTGRES,
                    name="pg_slow",
                    base_latency_s=0.5,
                    jitter_s=0.3,
                ),
                arm_at_s=2.0,
                hold_s=15.0,
            )
        ],
        duration_s=25.0,
        poll_interval_s=1.0,
        abort=AbortConditions(breach_tolerance=2),
        seed=seed,
    )


def object_store_partial_reads(seed: int = 1337) -> ChaosExperiment:
    """MinIO/S3 returns short bodies for expiring media — corruption-resilient?

    Hypothesis: partial reads of persisted video assets are detected and retried,
    so reader availability holds and the error rate stays low.
    """
    return ChaosExperiment.of(
        name="object_store_partial_reads",
        description="Object store truncates ~half of each media body.",
        hypothesis=SteadyStateHypothesis.of(
            [availability_at_least(0.95), error_rate_at_most(0.05)],
        ),
        blast_radius=[OBJECT_STORE],
        schedule=[
            ScheduledFault(
                PartialResponseFault(
                    dependency=OBJECT_STORE, name="short_read", keep_fraction=0.5
                ),
                arm_at_s=1.0,
                hold_s=12.0,
            ),
            ScheduledFault(
                ConnectionDropFault(dependency=OBJECT_STORE, name="reset", probability=0.2),
                arm_at_s=5.0,
                hold_s=8.0,
            ),
        ],
        duration_s=20.0,
        poll_interval_s=1.0,
        seed=seed,
    )


def provider_clock_skew(seed: int = 1337) -> ChaosExperiment:
    """The provider's clock drifts — do TTL/expiry-window assumptions hold?

    Hypothesis: a +120s skew on provider responses does not push availability
    below the floor (expiring task URLs are still refreshed in time).
    """
    return ChaosExperiment.of(
        name="provider_clock_skew",
        description="DashScope reports a +120s skewed clock for a window.",
        hypothesis=SteadyStateHypothesis.of([availability_at_least(0.97)]),
        blast_radius=[DASHSCOPE],
        schedule=[
            ScheduledFault(
                ClockSkewFault(dependency=DASHSCOPE, name="skew", skew_s_value=120.0),
                arm_at_s=1.0,
                hold_s=10.0,
            )
        ],
        duration_s=15.0,
        poll_interval_s=1.0,
        seed=seed,
    )


#: All built-in scenarios by name, for a CLI / API listing.
CATALOGUE = {
    fn.__name__: fn
    for fn in (
        provider_rate_limit_storm,
        redis_outage,
        postgres_latency_brownout,
        object_store_partial_reads,
        provider_clock_skew,
    )
}


__all__ = [
    "CATALOGUE",
    "DASHSCOPE",
    "OBJECT_STORE",
    "POSTGRES",
    "REDIS",
    "object_store_partial_reads",
    "postgres_latency_brownout",
    "provider_clock_skew",
    "provider_rate_limit_storm",
    "redis_outage",
]
