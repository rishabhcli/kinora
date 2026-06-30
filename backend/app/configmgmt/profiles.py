"""Environment profiles — presets, overlay/merge precedence, and diffing.

A *profile* is a named bundle of config overrides for an environment
(``local`` / ``test`` / ``staging`` / ``prod``). It does **not** replace
:class:`~app.core.config.Settings` — Settings remains the single source of truth
loaded from the environment. A profile is a small, reviewable overlay you can
*layer* under the live env so an operator sees exactly what a target environment
would change, and a CI check can diff "what prod expects" against "what's set".

Three operations:

* :func:`profile_for` — the built-in preset for an environment name.
* :func:`overlay` — merge a chain of layers with **last-wins precedence**, so
  ``overlay(base_profile, env_overrides)`` yields the effective settings map. A
  per-key provenance map records which layer won each key (for explainability).
* :func:`diff_profiles` — a structured field-by-field diff between two layers
  (added / removed / changed), for "staging vs prod" review and drift detection.

Everything is a pure transform over plain dicts — no Settings mutation, no I/O.
The presets intentionally carry only *posture* knobs (debug-ish toggles, safety
gates, log level, alert fractions) and never secrets — secrets come from the
environment / the secret backend, never a checked-in profile.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

__all__ = [
    "ProfileName",
    "Profile",
    "OverlayResult",
    "FieldChange",
    "ProfileDiff",
    "PROFILES",
    "profile_for",
    "overlay",
    "diff_profiles",
]


class ProfileName(StrEnum):
    """The four canonical environment profiles."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"

    @classmethod
    def coerce(cls, value: str) -> ProfileName:
        """Map an ``app_env`` string onto a profile, tolerating aliases.

        ``production`` -> ``prod``, ``stage``/``staging`` -> ``staging``; an
        unknown name raises so a typo'd ``APP_ENV`` can't silently pick the wrong
        posture.
        """
        norm = value.strip().lower()
        aliases = {
            "production": cls.PROD,
            "prod": cls.PROD,
            "stage": cls.STAGING,
            "staging": cls.STAGING,
            "local": cls.LOCAL,
            "dev": cls.LOCAL,
            "development": cls.LOCAL,
            "test": cls.TEST,
            "testing": cls.TEST,
            "ci": cls.TEST,
        }
        try:
            return aliases[norm]
        except KeyError:
            raise ValueError(
                f"unknown environment profile {value!r}; "
                f"expected one of {[p.value for p in cls]} (or a known alias)"
            ) from None


@dataclass(frozen=True, slots=True)
class Profile:
    """A named, immutable overlay of config overrides.

    ``values`` is a flat map of Settings field name -> override value. Profiles
    are merged (not Settings objects), so a value here is just data; it is
    validated when applied against the real :class:`Settings`.
    """

    name: ProfileName
    values: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """A mutable copy of the override map."""
        return dict(self.values)


# --------------------------------------------------------------------------- #
# Built-in presets. Posture only — never secrets.
# --------------------------------------------------------------------------- #
# These encode the *intended* posture per environment so a diff against the live
# env flags drift (e.g. prod with json logging off, or live-video armed without
# an explicit opt-in). They deliberately do NOT set DASHSCOPE_API_KEY, JWT_SECRET,
# or any credential — those are environment/secret-backend concerns.

_LOCAL = Profile(
    ProfileName.LOCAL,
    {
        "app_env": "local",
        "log_level": "DEBUG",
        "kinora_live_video": False,
        "analytics_enabled": True,
        "csrf_enabled": True,
    },
)

_TEST = Profile(
    ProfileName.TEST,
    {
        "app_env": "test",
        "log_level": "WARNING",
        "kinora_live_video": False,
        "reasoning_provider": "dashscope",
        "analytics_enabled": False,
    },
)

_STAGING = Profile(
    ProfileName.STAGING,
    {
        "app_env": "staging",
        "log_level": "INFO",
        "kinora_live_video": False,
        "csrf_enabled": True,
        "finops_soft_cap_fraction": 0.90,
    },
)

_PROD = Profile(
    ProfileName.PROD,
    {
        "app_env": "prod",
        "log_level": "INFO",
        # Live video stays OFF in the preset: arming it is an explicit, audited
        # opt-in (see app.configmgmt.safety), never a profile default.
        "kinora_live_video": False,
        "csrf_enabled": True,
        "mcp_validate_responses": True,
        "finops_soft_cap_fraction": 0.90,
    },
)

#: The built-in profile registry keyed by :class:`ProfileName`.
PROFILES: dict[ProfileName, Profile] = {
    ProfileName.LOCAL: _LOCAL,
    ProfileName.TEST: _TEST,
    ProfileName.STAGING: _STAGING,
    ProfileName.PROD: _PROD,
}


def profile_for(env: str | ProfileName) -> Profile:
    """Return the built-in preset for an environment name (alias-tolerant)."""
    name = env if isinstance(env, ProfileName) else ProfileName.coerce(env)
    return PROFILES[name]


@dataclass(frozen=True, slots=True)
class OverlayResult:
    """The effective map plus per-key provenance from an :func:`overlay`.

    Args:
        values: The merged map (last layer wins per key).
        provenance: ``field -> layer index`` recording which input layer supplied
            the winning value (0-based; the last contributor wins). Useful for
            "why is X set to Y?" explainability.
    """

    values: dict[str, Any]
    provenance: dict[str, int]

    def source_of(self, field_name: str) -> int | None:
        """The layer index that won ``field_name`` (or ``None`` if unset)."""
        return self.provenance.get(field_name)


def overlay(*layers: Mapping[str, Any] | Profile) -> OverlayResult:
    """Merge layers with **last-wins** precedence into an effective map.

    Each layer is either a flat mapping or a :class:`Profile` (its ``values`` are
    used). A later layer's key overrides an earlier one; a ``None`` value is a
    real override (it explicitly clears), not a skip. The returned
    :class:`OverlayResult` records which layer won each key so precedence is
    auditable.

    Typical use: ``overlay(profile_for("prod"), explicit_env_overrides)`` — the
    preset posture is the base, the deployment's explicit env wins on conflict.
    """
    merged: dict[str, Any] = {}
    provenance: dict[str, int] = {}
    for index, layer in enumerate(layers):
        items = layer.values if isinstance(layer, Profile) else layer
        for key, value in items.items():
            merged[key] = value
            provenance[key] = index
    return OverlayResult(values=merged, provenance=provenance)


class _Missing(Enum):
    """A distinct sentinel for "field absent in this layer" (vs a ``None`` value)."""

    SENTINEL = object()


#: The module-level "absent" marker, distinct from ``None`` (which is a real value).
_MISSING = _Missing.SENTINEL


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field-level difference between two layers."""

    field: str
    left: Any
    right: Any

    @property
    def kind(self) -> str:
        """``"added"`` (only on right), ``"removed"`` (only on left), or ``"changed"``."""
        if self.left is _MISSING:
            return "added"
        if self.right is _MISSING:
            return "removed"
        return "changed"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "kind": self.kind}
        if self.left is not _MISSING:
            out["left"] = self.left
        if self.right is not _MISSING:
            out["right"] = self.right
        return out


@dataclass(frozen=True, slots=True)
class ProfileDiff:
    """A structured diff between two config layers ("left" -> "right")."""

    changes: tuple[FieldChange, ...]

    @property
    def added(self) -> tuple[FieldChange, ...]:
        return tuple(c for c in self.changes if c.kind == "added")

    @property
    def removed(self) -> tuple[FieldChange, ...]:
        return tuple(c for c in self.changes if c.kind == "removed")

    @property
    def changed(self) -> tuple[FieldChange, ...]:
        return tuple(c for c in self.changes if c.kind == "changed")

    @property
    def is_empty(self) -> bool:
        """True when the two layers are field-for-field identical."""
        return not self.changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": [c.to_dict() for c in self.added],
            "removed": [c.to_dict() for c in self.removed],
            "changed": [c.to_dict() for c in self.changed],
        }


def diff_profiles(
    left: Mapping[str, Any] | Profile,
    right: Mapping[str, Any] | Profile,
    *,
    only: Sequence[str] | None = None,
) -> ProfileDiff:
    """Diff two layers field-by-field (added / removed / changed).

    Args:
        left: The baseline layer (a :class:`Profile` or a flat mapping).
        right: The compared layer.
        only: When given, restrict the diff to these field names (e.g. compare
            just the safety-relevant knobs between staging and prod).

    Returns a :class:`ProfileDiff` with the changes sorted by field name for a
    deterministic, review-friendly output.
    """
    left_map = left.values if isinstance(left, Profile) else left
    right_map = right.values if isinstance(right, Profile) else right
    keys = set(left_map) | set(right_map)
    if only is not None:
        keys &= set(only)

    changes: list[FieldChange] = []
    for key in sorted(keys):
        lv = left_map.get(key, _MISSING)
        rv = right_map.get(key, _MISSING)
        if lv == rv:
            continue
        changes.append(FieldChange(field=key, left=lv, right=rv))
    return ProfileDiff(changes=tuple(changes))
