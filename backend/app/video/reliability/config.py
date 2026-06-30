"""Tunables for the reliability coordinator (additive, self-contained).

A frozen pydantic-v2 model so it validates its own invariants and can be built
from the global ``Settings`` by the orchestrator later (``from_settings``) without
this subsystem importing or mutating the shared config. Defaults are conservative
and spend-safe: bounded retries, a real per-shot deadline, and a quality floor
that escalates rather than ships garbage. Nothing here flips ``KINORA_LIVE_VIDEO``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReliabilityConfig(BaseModel):
    """Coordinator policy knobs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_providers: int = Field(
        default=4,
        ge=1,
        description="Cap on ranked candidates attempted before falling back.",
    )
    per_provider_attempts: int = Field(
        default=1,
        ge=1,
        description="Coordinator-level retries of one provider (router does its own too).",
    )
    retry_backoff_base_s: float = Field(
        default=0.25, ge=0.0, description="Base delay for coordinator-level provider retries."
    )
    retry_backoff_max_s: float = Field(default=4.0, ge=0.0)
    default_deadline_s: float = Field(
        default=30.0, gt=0.0, description="Used when a shot does not set its own deadline."
    )
    default_min_quality: float = Field(default=0.6, ge=0.0, le=1.0)
    # Ranking weights: score = w_rep*reputation + w_load*(1-load) - w_cost*norm_cost.
    weight_reputation: float = Field(default=1.0, ge=0.0)
    weight_load_headroom: float = Field(default=0.5, ge=0.0)
    weight_cost: float = Field(default=0.25, ge=0.0)
    enable_fallback_card: bool = Field(
        default=True,
        description="Synthesize a degraded narrated-text card when all else fails.",
    )

    @classmethod
    def from_settings(cls, settings: Any) -> ReliabilityConfig:
        """Build from a global ``Settings`` object, reading any present attrs.

        Tolerant by design: unknown/absent settings fall back to the defaults, so
        the orchestrator can adopt this incrementally without a config migration.
        """

        def _get(name: str, default: Any) -> Any:
            return getattr(settings, name, default)

        defaults = cls.model_fields
        return cls(
            max_providers=_get(
                "video_reliability_max_providers", defaults["max_providers"].default
            ),
            per_provider_attempts=_get(
                "video_reliability_per_provider_attempts",
                defaults["per_provider_attempts"].default,
            ),
            default_deadline_s=_get(
                "video_reliability_deadline_s", defaults["default_deadline_s"].default
            ),
            default_min_quality=_get(
                "video_reliability_min_quality", defaults["default_min_quality"].default
            ),
            enable_fallback_card=_get(
                "video_reliability_fallback_card", defaults["enable_fallback_card"].default
            ),
        )


__all__ = ["ReliabilityConfig"]
