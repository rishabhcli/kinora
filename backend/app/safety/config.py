"""Additive, env-driven configuration for the safety gateway.

Kept **separate** from the global :class:`app.core.config.Settings` so the gateway
adds no fields to the shared schema (the coordination rule: settings are
additive). A :class:`SafetySettings` reads its own ``SAFETY_*`` env block with
pydantic-settings, defaults to the safe/offline posture, and never requires any
secret — importing it forces no network and no provider dependency.

All defaults are conservative: the model classifier lane is *off* (the
deterministic keyword classifier is used) unless explicitly enabled, so the unit
suite and an offline run never touch a provider.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class SafetySettings(BaseSettings):
    """Strongly-typed safety-gateway configuration (``SAFETY_*`` env block)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SAFETY_",
        case_sensitive=False,
        extra="ignore",
    )

    #: Master switch. When False the gateway is a no-op pass-through (still logs a
    #: decision), so a deployment can ship it dark and turn it on per-environment.
    enabled: bool = True

    #: Use the model-backed classifier/softener lanes. OFF by default so tests and
    #: offline runs use the deterministic keyword fakes — no network, no spend.
    #: The composition root only wires the model lane when this is True AND
    #: providers are available.
    use_model_classifier: bool = False
    use_model_softener: bool = False

    #: Whether the gateway attempts intent-preserving auto-softening at all. When
    #: False a softenable prompt is quarantined instead of transformed.
    enable_softening: bool = True

    #: Post-generation output gate fail posture. False = fail-open (a degraded
    #: classifier allows the clip, since the prompt was pre-screened).
    output_fail_closed: bool = False

    #: Max number of sampled clip frames the output gate inspects (bounds the VL
    #: request). The classifier seam owns the actual sampling.
    max_sampled_frames: int = 4

    #: Default per-book policy strictness profile name (reserved for future
    #: per-tenant policy tables; the baseline policy is used today).
    policy_version: str = "default"


@lru_cache
def get_safety_settings() -> SafetySettings:
    """Cached :class:`SafetySettings` (mirrors ``app.core.config.get_settings``)."""
    return SafetySettings()


__all__ = ["SafetySettings", "get_safety_settings"]
