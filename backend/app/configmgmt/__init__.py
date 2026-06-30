"""Kinora configuration-management plane: validation, profiles, secrets, safety.

A production-grade layer that sits **beside** :class:`~app.core.config.Settings`
(never replacing it) and answers four operational questions about the live
configuration:

* **Is it coherent?** :class:`ConfigValidator` runs a suite of cross-field
  invariants — live-video spend guards, video-backend keys, S3/public-URL
  coherence, scheduler watermark ordering, FinOps fractions, MCP auth — and rolls
  them into a typed :class:`ReadinessVerdict` (it reports; it never raises and
  never enables anything).
* **Is it safe to boot in prod?** :class:`ProdSafetyGate` enforces the
  non-negotiables — no demo credentials, no insecure placeholders, no debug/chaos
  posture, and ``KINORA_LIVE_VIDEO`` only with an explicit prod opt-in — raising
  :class:`ProdSafetyError` (with *every* violation) so an unsafe process dies at
  startup. ``KINORA_LIVE_VIDEO`` is treated strictly as a guarded gate; this
  plane never recommends or enables it.
* **What does each environment expect?** :func:`profile_for` gives the built-in
  ``local``/``test``/``staging``/``prod`` presets; :func:`overlay` merges layers
  with last-wins precedence (and records provenance); :func:`diff_profiles`
  produces a structured added/removed/changed diff for drift review.
* **Where do secrets come from, safely?** :class:`SecretResolver` over a
  pluggable :class:`SecretBackend` chain (env / file / static / vault) with TTL
  caching, rotation hooks, and a :class:`SecretValue` that refuses to print
  itself; :func:`redacted_dump` gives a secret-masked view of the whole config
  for safe introspection.

Public entrypoints are re-exported here so callers write
``from app.configmgmt import validate_settings, assert_safe_to_boot, ...``.
See ``DESIGN.md`` for the architecture and the boot-path wiring sketch.
"""

from __future__ import annotations

from app.configmgmt.errors import ConfigFinding, ProdSafetyError, Severity
from app.configmgmt.profiles import (
    PROFILES,
    FieldChange,
    OverlayResult,
    Profile,
    ProfileDiff,
    ProfileName,
    diff_profiles,
    overlay,
    profile_for,
)
from app.configmgmt.redaction import (
    ALWAYS_REDACT_FIELDS,
    is_secret_field,
    redact_mapping,
    redacted_dump,
)
from app.configmgmt.safety import (
    CHAOS_ARMED_ENV,
    DEMO_EMAIL,
    DEMO_PASSWORD,
    PROD_LIVE_VIDEO_OPT_IN_ENV,
    ProdSafetyGate,
    ProdSafetyReport,
    assert_safe_to_boot,
)
from app.configmgmt.secrets import (
    EnvSecretBackend,
    FileSecretBackend,
    RotationHook,
    SecretBackend,
    SecretNotFoundError,
    SecretRef,
    SecretResolver,
    SecretValue,
    StaticSecretBackend,
    env_resolver,
)
from app.configmgmt.validator import (
    INVARIANTS,
    ConfigValidator,
    ReadinessVerdict,
    validate_settings,
)

__all__ = [
    # errors / vocabulary
    "ConfigFinding",
    "ProdSafetyError",
    "Severity",
    # validator
    "ConfigValidator",
    "ReadinessVerdict",
    "validate_settings",
    "INVARIANTS",
    # safety
    "ProdSafetyGate",
    "ProdSafetyReport",
    "assert_safe_to_boot",
    "DEMO_EMAIL",
    "DEMO_PASSWORD",
    "PROD_LIVE_VIDEO_OPT_IN_ENV",
    "CHAOS_ARMED_ENV",
    # profiles
    "Profile",
    "ProfileName",
    "PROFILES",
    "profile_for",
    "overlay",
    "OverlayResult",
    "diff_profiles",
    "ProfileDiff",
    "FieldChange",
    # secrets
    "SecretRef",
    "SecretValue",
    "SecretBackend",
    "SecretResolver",
    "SecretNotFoundError",
    "EnvSecretBackend",
    "FileSecretBackend",
    "StaticSecretBackend",
    "RotationHook",
    "env_resolver",
    # redaction
    "redacted_dump",
    "redact_mapping",
    "is_secret_field",
    "ALWAYS_REDACT_FIELDS",
]
