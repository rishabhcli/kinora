"""Additive, self-contained settings for the service-mesh contract layer.

Kept local to this package (not folded into ``app/core/config.py``) so the
subsystem stays additive and import-cheap. All fields have safe defaults so the
mesh boots with zero configuration; env overrides use the ``SERVICEMESH_`` prefix.

The knobs are deliberately conservative:

* ``default_compatibility`` — the contract a channel is held to when one is not
  declared explicitly. BACKWARD is the production-safe default (a new consumer can
  always read an old producer).
* ``enforce_gate`` — master switch for the CI compatibility gate. On by default; a
  team can flip it off on a dev/experimental deployment.
* ``stable_only`` — honour the pre-1.0 "minor may break" convention.
* ``validate_payloads`` — whether the consumer dispatcher structurally validates
  payloads before routing.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.servicemesh.compatibility import CompatibilityMode

__all__ = ["ServiceMeshSettings", "get_servicemesh_settings"]


class ServiceMeshSettings(BaseSettings):
    """Tunables for the message-contract / schema-versioning layer."""

    model_config = SettingsConfigDict(
        env_prefix="SERVICEMESH_",
        extra="ignore",
        frozen=True,
    )

    default_compatibility: CompatibilityMode = CompatibilityMode.BACKWARD
    enforce_gate: bool = True
    stable_only: bool = True
    validate_payloads: bool = True
    # Soft ceiling on a single conversion chain length (defensive; a sane fleet
    # never needs more hops than it has historical versions).
    max_conversion_hops: int = 16


@lru_cache
def get_servicemesh_settings() -> ServiceMeshSettings:
    """Process-wide cached service-mesh settings."""
    return ServiceMeshSettings()
