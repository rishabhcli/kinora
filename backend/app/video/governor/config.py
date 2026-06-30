"""Governor configuration — per-provider profiles, assembled with no env reads.

A :class:`ProviderProfile` bundles the four governor sub-configs for one provider
(quota limits, SLA objective, throttle pacing, fair-share weighting defaults). A
:class:`GovernorConfig` maps provider name → profile with a default fallback, so a
provider with no explicit profile still gets sane unbounded-quota/lenient-SLA
behaviour rather than crashing.

This module is pure data: it imports no settings and reads no environment. The
composition root translates ``Settings`` (e.g. ``budget_ceiling_video_s``,
``video_poll_timeout_s``) into a :class:`GovernorConfig` and injects it; tests build
profiles inline. :func:`default_video_profiles` seeds the providers Kinora ships
against (DashScope Wan, MiniMax Hailuo) from the documented limits, but only as a
*starting point* — real ceilings come from settings/observation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .fairshare import FairShareConfig
from .quota import QuotaLimits
from .sla import SlaObjective
from .throttle import ThrottleConfig


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """The full governance profile for one video provider."""

    quota: QuotaLimits = field(default_factory=QuotaLimits)
    sla: SlaObjective = field(default_factory=SlaObjective)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    #: Default fair-share weight for tenants submitting to this provider.
    default_tenant_weight: float = 1.0


@dataclass
class GovernorConfig:
    """Per-provider profiles plus shared fair-share + default fallbacks."""

    profiles: dict[str, ProviderProfile] = field(default_factory=dict)
    default_profile: ProviderProfile = field(default_factory=ProviderProfile)
    fairshare: FairShareConfig = field(default_factory=FairShareConfig)

    def profile_for(self, provider: str) -> ProviderProfile:
        return self.profiles.get(provider, self.default_profile)

    def set_profile(self, provider: str, profile: ProviderProfile) -> None:
        self.profiles[provider] = profile


def default_video_profiles() -> GovernorConfig:
    """A starting :class:`GovernorConfig` for the providers Kinora ships against.

    Values are conservative defaults drawn from the documented behaviour (the
    DashScope-intl free tier's ~1,650 video-second pool and rpm throttling; the
    MiniMax per-clip USD cap). They are *defaults*, not authority — a deployment
    overrides them from ``Settings``/observation. The unknown-provider fallback is
    unbounded quota + a lenient SLA so an unmodelled backend still routes.
    """
    return GovernorConfig(
        profiles={
            "dashscope": ProviderProfile(
                quota=QuotaLimits(
                    requests_per_min=20,
                    concurrent_jobs=4,
                    daily_video_seconds=1650.0,  # the §11.1 lifetime pool, per day cap
                    monthly_spend_usd=None,
                ),
                sla=SlaObjective(
                    target_success_rate=0.95,
                    target_p95_latency_ms=120_000.0,  # Wan async renders are slow
                    window_size=100,
                ),
                throttle=ThrottleConfig(rate_per_min=20.0, burst=4),
            ),
            "minimax": ProviderProfile(
                quota=QuotaLimits(
                    requests_per_min=10,
                    concurrent_jobs=3,
                    daily_video_seconds=None,
                    monthly_spend_usd=30.0,  # the §11.1 belt-and-suspenders USD cap
                ),
                sla=SlaObjective(
                    target_success_rate=0.95,
                    target_p95_latency_ms=90_000.0,
                    window_size=100,
                ),
                throttle=ThrottleConfig(rate_per_min=10.0, burst=3),
            ),
        },
    )


__all__ = [
    "GovernorConfig",
    "ProviderProfile",
    "default_video_profiles",
]
