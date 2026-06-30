"""The override layer — runtime decisions that overlay the Settings base.

Three kinds of overlay, in increasing specificity:

1. :class:`StaticOverride` — "for everyone, flag X = V" (a global runtime flip,
   e.g. an operator turns ``provider_gateway_enabled`` on without a redeploy).
2. :class:`TargetingRule` — "for contexts matching these dimensions, flag X = V"
   keyed by the four Kinora targeting dimensions (book / user / cohort /
   provider). Rules carry a numeric ``specificity`` (count of constrained
   dimensions) so the resolver can pick the *most specific* match.
3. :class:`PercentRollout` — "ramp flag X to V for P% of a bucketing unit",
   bucketed deterministically (sticky) by :func:`app.flags.hashing.bucket_bp`.

A :class:`OverrideLayer` holds, per flag key, an optional static override, an
ordered list of targeting rules, and an optional rollout. It is an immutable
value object: every mutation returns a new layer (and bumps ``version``), so a
resolver reading a layer never races a writer. The whole layer round-trips
to/from a plain ``dict`` for persistence + snapshot/export.

Pure: reuses only :func:`app.flags.hashing.bucket_bp` and the plane's own types.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.flags.hashing import bucket_bp
from app.flags.plane.context import FlagContext
from app.flags.plane.spec import FlagValue

#: The targeting dimensions Kinora flags can match on. A rule constrains a
#: subset; an unconstrained dimension matches anything.
TARGET_DIMENSIONS = ("book", "user", "cohort", "provider")


@dataclass(frozen=True, slots=True)
class StaticOverride:
    """A global runtime value for a flag (applies to every context)."""

    value: FlagValue

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}


@dataclass(frozen=True, slots=True)
class TargetingRule:
    """A value served when *every* constrained dimension matches the context.

    A dimension left ``None`` is unconstrained (matches anything). ``specificity``
    is the number of constrained dimensions; the resolver sorts matching rules by
    it descending (then by ``priority``) so the most specific rule wins. The
    optional ``rollout`` lets a rule *also* gate to a percentage of its matched
    audience (e.g. "for cohort=beta, ramp to 25%").
    """

    id: str
    value: FlagValue
    book: str | None = None
    user: str | None = None
    cohort: str | None = None
    provider: str | None = None
    priority: int = 0
    rollout: PercentRollout | None = None
    description: str = ""

    @property
    def specificity(self) -> int:
        """How many targeting dimensions this rule constrains (0..4)."""
        return sum(
            1
            for dim in (self.book, self.user, self.cohort, self.provider)
            if dim is not None
        )

    def matches(self, context: FlagContext) -> bool:
        """True when every constrained dimension equals the context's value."""
        for dim in TARGET_DIMENSIONS:
            constraint = getattr(self, dim)
            if constraint is None:
                continue
            if context.dimension(dim) != constraint:
                return False
        return self.rollout is None or self.rollout.admits(context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value": self.value,
            "book": self.book,
            "user": self.user,
            "cohort": self.cohort,
            "provider": self.provider,
            "priority": self.priority,
            "rollout": self.rollout.to_dict() if self.rollout else None,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TargetingRule:
        rollout = data.get("rollout")
        return cls(
            id=str(data["id"]),
            value=data.get("value"),
            book=data.get("book"),
            user=data.get("user"),
            cohort=data.get("cohort"),
            provider=data.get("provider"),
            priority=int(data.get("priority", 0)),
            rollout=PercentRollout.from_dict(rollout) if rollout else None,
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class PercentRollout:
    """A deterministic, sticky percentage gate on the *current* unit.

    Admits a context iff its deterministic bucket (over the chosen ``bucket_by``
    unit, salted by the flag key + optional ``seed``) falls below ``percent``.
    Because the bucketing is monotone in ``percent`` (see
    :func:`app.flags.hashing.in_rollout`), ramping 10% -> 25% only ever *adds*
    units: a unit admitted at 10% is still admitted at 25%, so a reader never
    flaps in and out of a rollout as it grows.
    """

    flag_key: str
    percent: float  # 0..100
    bucket_by: str = "user"  # which targeting dimension is the bucketing unit
    seed: int = 0

    def __post_init__(self) -> None:
        if self.bucket_by not in TARGET_DIMENSIONS and self.bucket_by != "key":
            raise ValueError(
                f"rollout bucket_by must be a targeting dimension, got {self.bucket_by!r}"
            )

    def _salt(self) -> str:
        base = f"plane:{self.flag_key}"
        return f"{base}#{self.seed}" if self.seed else base

    def admits(self, context: FlagContext) -> bool:
        """Whether ``context`` is inside this rollout (sticky, deterministic)."""
        if self.percent <= 0:
            return False
        unit = context.unit_for(self.bucket_by)
        if not unit:
            # No identity on the bucketing dimension -> exclude (fail safe: a
            # ramp must not silently capture un-bucketable / anonymous traffic,
            # even at 100%, since it cannot be assigned a sticky bucket).
            return False
        if self.percent >= 100:
            return True
        threshold_bp = round(self.percent * 100)
        return bucket_bp(unit, self._salt()) < threshold_bp

    def to_dict(self) -> dict[str, Any]:
        return {
            "flag_key": self.flag_key,
            "percent": self.percent,
            "bucket_by": self.bucket_by,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PercentRollout:
        return cls(
            flag_key=str(data["flag_key"]),
            percent=float(data["percent"]),
            bucket_by=str(data.get("bucket_by", "user")),
            seed=int(data.get("seed", 0)),
        )


@dataclass(frozen=True, slots=True)
class FlagOverlay:
    """All runtime overlays for a single flag (static + rules + rollout)."""

    static: StaticOverride | None = None
    rules: tuple[TargetingRule, ...] = ()
    rollout: PercentRollout | None = None

    def is_empty(self) -> bool:
        return self.static is None and not self.rules and self.rollout is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "static": self.static.to_dict() if self.static else None,
            "rules": [r.to_dict() for r in self.rules],
            "rollout": self.rollout.to_dict() if self.rollout else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlagOverlay:
        static = data.get("static")
        rollout = data.get("rollout")
        return cls(
            static=StaticOverride(static["value"]) if static else None,
            rules=tuple(TargetingRule.from_dict(r) for r in data.get("rules", ())),
            rollout=PercentRollout.from_dict(rollout) if rollout else None,
        )


@dataclass(frozen=True, slots=True)
class OverrideLayer:
    """An immutable, versioned set of per-flag overlays — the runtime layer.

    Every mutation (set/clear static, add/remove rule, set rollout) returns a new
    ``OverrideLayer`` with ``version`` incremented, so the resolver always reads
    a consistent snapshot and the plane can detect change for subscriptions.
    """

    overlays: dict[str, FlagOverlay] = field(default_factory=dict)
    version: int = 0

    def overlay_for(self, key: str) -> FlagOverlay:
        """The overlay for ``key`` (an empty overlay if none)."""
        return self.overlays.get(key, FlagOverlay())

    def _with(self, key: str, overlay: FlagOverlay) -> OverrideLayer:
        new = dict(self.overlays)
        if overlay.is_empty():
            new.pop(key, None)
        else:
            new[key] = overlay
        return OverrideLayer(overlays=new, version=self.version + 1)

    def set_static(self, key: str, value: FlagValue) -> OverrideLayer:
        """Set the global static override for ``key``."""
        overlay = replace(self.overlay_for(key), static=StaticOverride(value))
        return self._with(key, overlay)

    def clear_static(self, key: str) -> OverrideLayer:
        """Remove the global static override for ``key`` (no-op if absent)."""
        overlay = replace(self.overlay_for(key), static=None)
        return self._with(key, overlay)

    def add_rule(self, key: str, rule: TargetingRule) -> OverrideLayer:
        """Add (or replace, by id) a targeting rule for ``key``."""
        existing = tuple(r for r in self.overlay_for(key).rules if r.id != rule.id)
        overlay = replace(self.overlay_for(key), rules=(*existing, rule))
        return self._with(key, overlay)

    def remove_rule(self, key: str, rule_id: str) -> OverrideLayer:
        """Remove the rule with ``rule_id`` from ``key`` (no-op if absent)."""
        existing = tuple(r for r in self.overlay_for(key).rules if r.id != rule_id)
        overlay = replace(self.overlay_for(key), rules=existing)
        return self._with(key, overlay)

    def set_rollout(self, key: str, rollout: PercentRollout | None) -> OverrideLayer:
        """Set (or clear, with ``None``) the percentage rollout for ``key``."""
        overlay = replace(self.overlay_for(key), rollout=rollout)
        return self._with(key, overlay)

    def clear_flag(self, key: str) -> OverrideLayer:
        """Drop every overlay for ``key`` (revert it fully to its base)."""
        if key not in self.overlays:
            return self
        new = {k: v for k, v in self.overlays.items() if k != key}
        return OverrideLayer(overlays=new, version=self.version + 1)

    def keys(self) -> tuple[str, ...]:
        """The flag keys carrying any overlay (sorted)."""
        return tuple(sorted(self.overlays))

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable projection for persistence / snapshot export."""
        return {
            "version": self.version,
            "overlays": {k: v.to_dict() for k, v in sorted(self.overlays.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverrideLayer:
        overlays = {
            k: FlagOverlay.from_dict(v) for k, v in data.get("overlays", {}).items()
        }
        return cls(overlays=overlays, version=int(data.get("version", 0)))


#: Re-export so callers can reference the dimension order from one place.
__all__ = [
    "TARGET_DIMENSIONS",
    "FlagOverlay",
    "OverrideLayer",
    "PercentRollout",
    "StaticOverride",
    "TargetingRule",
]
