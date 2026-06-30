"""The layered resolver — base (Settings) -> override -> targeting, most-specific wins.

Given a :class:`~app.flags.plane.registry.FlagRegistry` (the typed specs + their
live base values) and an :class:`~app.flags.plane.overrides.OverrideLayer` (the
runtime overlays), :class:`LayeredResolver` answers "what is the effective value
of flag X for this :class:`~app.flags.plane.context.FlagContext`?" as a *total*
function: it never raises into the caller, always returns a typed
:class:`Resolution` carrying the value, the layer that produced it, and a reason.

Resolution order, lowest precedence first:

1. **base** — the flag's :attr:`FlagSpec.default` (the live Settings value).
2. **static override** — a global runtime flip, if present.
3. **targeting rules** — every rule whose dimensions match the context; the most
   specific (most constrained, ties broken by ``priority`` then ``id``) wins.
4. **rollout** — if a percentage rollout is configured and admits the context,
   it serves *on*; if it is configured and excludes the context it pins the
   value back toward the base of a boolean (so a ramp can only *add* the on-value
   to its admitted fraction, never flip the base).

The kill-switch :class:`~app.flags.plane.safety.KillSwitchGuard` clamps the final
value so a guarded flag can never be served raised, whatever the layers say.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.flags.plane.context import FlagContext
from app.flags.plane.overrides import OverrideLayer, TargetingRule
from app.flags.plane.safety import KillSwitchGuard
from app.flags.plane.spec import FlagSpec, FlagType, FlagValue


class ResolutionSource(StrEnum):
    """Which layer produced the resolved value (attached to every result)."""

    BASE = "base"  # the spec default / Settings value
    STATIC_OVERRIDE = "static_override"  # a global runtime flip
    TARGETING_RULE = "targeting_rule"  # a matched, most-specific rule
    ROLLOUT = "rollout"  # a percentage ramp admitted this context
    KILL_SWITCH_CLAMP = "kill_switch_clamp"  # safety clamped a raised value down
    UNKNOWN_FLAG = "unknown_flag"  # the key is not registered


@dataclass(frozen=True, slots=True)
class Resolution:
    """The total result of resolving one flag for one context."""

    key: str
    value: FlagValue
    source: ResolutionSource
    rule_id: str | None = None
    #: The pre-clamp value the layers produced (differs from ``value`` only when
    #: the kill-switch guard clamped it). Useful for the admin "why" panel.
    raw_value: FlagValue = None
    in_rollout: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source.value,
            "rule_id": self.rule_id,
            "raw_value": self.raw_value,
            "in_rollout": self.in_rollout,
        }


class LayeredResolver:
    """Resolve flags by overlaying an :class:`OverrideLayer` on registry bases."""

    def __init__(self, guard: KillSwitchGuard | None = None) -> None:
        self._guard = guard or KillSwitchGuard()

    def resolve(
        self,
        spec: FlagSpec,
        layer: OverrideLayer,
        context: FlagContext,
    ) -> Resolution:
        """Resolve ``spec`` for ``context`` against ``layer`` (never raises)."""
        try:
            return self._resolve(spec, layer, context)
        except Exception:  # noqa: BLE001 - total function: degrade to safe base
            return Resolution(
                key=spec.key,
                value=spec.default,
                source=ResolutionSource.BASE,
                raw_value=spec.default,
            )

    def _resolve(
        self, spec: FlagSpec, layer: OverrideLayer, context: FlagContext
    ) -> Resolution:
        base = spec.default
        overlay = layer.overlay_for(spec.key)

        raw: FlagValue = base
        source = ResolutionSource.BASE
        rule_id: str | None = None
        in_rollout = False

        # Layer 2: a global static override beats the base.
        if overlay.static is not None:
            raw = overlay.static.value
            source = ResolutionSource.STATIC_OVERRIDE

        # Layer 3: the most specific matching targeting rule beats the static.
        matched = self._best_rule(overlay.rules, context)
        if matched is not None:
            raw = matched.value
            source = ResolutionSource.TARGETING_RULE
            rule_id = matched.id
            if matched.rollout is not None:
                in_rollout = True  # the rule matched *because* its rollout admitted

        # Layer 4: a flag-level percentage rollout. It only ever serves the
        # rollout value to its admitted fraction; excluded contexts keep whatever
        # the lower layers produced (so a ramp adds reach, never removes it).
        if overlay.rollout is not None:
            admits = overlay.rollout.admits(context)
            in_rollout = in_rollout or admits
            if admits:
                raw = self._rollout_on_value(spec)
                source = ResolutionSource.ROLLOUT

        coerced = spec.coerce(raw)
        clamped = self._guard.clamp(spec, base, coerced)
        if clamped != coerced:
            return Resolution(
                key=spec.key,
                value=clamped,
                source=ResolutionSource.KILL_SWITCH_CLAMP,
                rule_id=rule_id,
                raw_value=coerced,
                in_rollout=in_rollout,
            )
        return Resolution(
            key=spec.key,
            value=clamped,
            source=source,
            rule_id=rule_id,
            raw_value=coerced,
            in_rollout=in_rollout,
        )

    @staticmethod
    def _best_rule(
        rules: tuple[TargetingRule, ...], context: FlagContext
    ) -> TargetingRule | None:
        """The most-specific matching rule (ties: higher priority, then id)."""
        matches = [r for r in rules if r.matches(context)]
        if not matches:
            return None
        return max(matches, key=lambda r: (r.specificity, r.priority, r.id))

    @staticmethod
    def _rollout_on_value(spec: FlagSpec) -> FlagValue:
        """The value a flag-level rollout serves to admitted contexts.

        A boolean flag ramps *on*; for any other type the rollout is meaningful
        only paired with a rule value, so a bare flag-level rollout on a non-bool
        flag falls back to the base (it cannot invent a value).
        """
        if spec.type is FlagType.BOOL:
            return True
        return spec.default


__all__ = ["LayeredResolver", "Resolution", "ResolutionSource"]
