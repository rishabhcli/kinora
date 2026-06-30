"""The unified runtime feature-flag / dynamic-config plane (``app.flags.plane``).

Kinora's behaviour is gated by a sprawl of boolean / scalar knobs — the
``KINORA_LIVE_VIDEO`` spend gate, ``provider_gateway_enabled``,
``video_backend``, ``analytics_enabled``, the §9.7 render-hardening toggles, the
scheduler watermarks — each read straight off :class:`~app.core.config.Settings`
with an ad-hoc ``if settings.x`` at the call site. That is fine for *static*
config but gives operators no way to flip behaviour for a single book / cohort /
provider at runtime, no audit of who changed what, and no single place to ask
"what is the effective value of flag X for this context?".

This plane is that single place. It is **strictly additive and read-through**:

* **Settings is the one base source.** Every flag is bound to a typed default
  (usually a Settings field) — the plane never invents config, it *overlays*
  runtime decisions on top of the same values the rest of the app reads.
* **Layered resolution, most-specific wins.** ``base (Settings) -> persisted
  override -> targeting rules``. Targeting matches by book / user / cohort /
  provider and supports **deterministic, sticky percentage rollouts** (reusing
  the §13 :func:`app.flags.hashing.bucket_bp` bucketing so a unit never flaps).
* **A kill-switch safety layer.** A flag declared a *guarded kill-switch* (e.g.
  ``KINORA_LIVE_VIDEO``) can only ever be forced **down** from its base — the
  plane physically refuses to resolve or persist a value that raises it. Live
  video can never be turned on by accident through this surface.
* **Validated + audited writes.** Overrides/rules are type-checked against the
  flag spec and every change is diffed (reusing :mod:`app.flags.audit`) into an
  audit record.
* **Hot-reload + change subscription.** Subscribers (the scheduler, a cache, an
  SSE bridge) are notified synchronously on any change.
* **Snapshot / export.** The full effective configuration can be exported for a
  given context, and the override layer round-trips to/from a plain dict.

The pure core (``spec``, ``registry``, ``overrides``, ``resolution``,
``safety``, ``subscriptions``) imports no infra and is exhaustively unit-tested
with no network/DB. :class:`RuntimeConfigPlane` is the facade; :mod:`app.flags.plane.api`
is the optional admin router.
"""

from __future__ import annotations

from app.flags.plane.errors import (
    FlagTypeError,
    KillSwitchViolation,
    PlaneError,
    UnknownFlagError,
)
from app.flags.plane.overrides import (
    OverrideLayer,
    PercentRollout,
    StaticOverride,
    TargetingRule,
)
from app.flags.plane.plane import RuntimeConfigPlane
from app.flags.plane.registry import FlagRegistry, build_default_registry
from app.flags.plane.resolution import (
    LayeredResolver,
    Resolution,
    ResolutionSource,
)
from app.flags.plane.safety import KillSwitchGuard
from app.flags.plane.spec import FlagSpec, FlagType
from app.flags.plane.subscriptions import ChangeEvent, ChangeKind, SubscriptionHub

__all__ = [
    "ChangeEvent",
    "ChangeKind",
    "FlagRegistry",
    "FlagSpec",
    "FlagType",
    "FlagTypeError",
    "KillSwitchGuard",
    "KillSwitchViolation",
    "LayeredResolver",
    "OverrideLayer",
    "PercentRollout",
    "PlaneError",
    "Resolution",
    "ResolutionSource",
    "RuntimeConfigPlane",
    "StaticOverride",
    "SubscriptionHub",
    "TargetingRule",
    "UnknownFlagError",
    "build_default_registry",
]
