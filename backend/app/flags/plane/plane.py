"""``RuntimeConfigPlane`` — the facade that unifies the runtime config plane.

One object wires the pieces together:

* a :class:`~app.flags.plane.registry.FlagRegistry` (the typed catalog, bound to
  the live :class:`Settings` so the base layer *is* Settings);
* a :class:`~app.flags.plane.store.OverrideStore` (where overlays + audit live);
* a :class:`~app.flags.plane.resolution.LayeredResolver` (base -> override ->
  targeting -> rollout, most-specific wins);
* a :class:`~app.flags.plane.safety.KillSwitchGuard` (a guarded flag can only be
  forced down);
* a :class:`~app.flags.plane.subscriptions.SubscriptionHub` (hot-reload notify).

The **read** API is typed and total — :meth:`is_enabled`, :meth:`get_int`,
:meth:`get_float`, :meth:`get_string`, :meth:`get_json`, :meth:`get` (the raw
:class:`Resolution`). It never raises; an unknown key falls back to a supplied
default.

The **write** API is validated + audited + notified: every override / rule /
rollout value is coerced to the flag's type and run through the kill-switch guard
(so a write that would raise ``kinora.live_video`` is rejected with
:class:`KillSwitchViolation`), the new layer is persisted with a structural audit
record, and subscribers are notified.

The plane is synchronous and infra-free with the default in-memory store, so the
whole thing is unit-testable with no network/DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.flags.plane.audit import PlaneAuditRecord, build_record
from app.flags.plane.context import EMPTY_CONTEXT, FlagContext
from app.flags.plane.overrides import (
    OverrideLayer,
    PercentRollout,
    TargetingRule,
)
from app.flags.plane.registry import FlagRegistry, build_default_registry
from app.flags.plane.resolution import LayeredResolver, Resolution, ResolutionSource
from app.flags.plane.safety import KillSwitchGuard
from app.flags.plane.spec import FlagSpec, FlagValue
from app.flags.plane.store import InMemoryOverrideStore, OverrideStore
from app.flags.plane.subscriptions import ChangeEvent, ChangeKind, SubscriptionHub

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import Settings


class RuntimeConfigPlane:
    """The unified runtime feature-flag / dynamic-config plane facade."""

    def __init__(
        self,
        registry: FlagRegistry,
        *,
        store: OverrideStore | None = None,
        guard: KillSwitchGuard | None = None,
        resolver: LayeredResolver | None = None,
        hub: SubscriptionHub | None = None,
    ) -> None:
        self._registry = registry
        self._store = store or InMemoryOverrideStore()
        self._guard = guard or KillSwitchGuard()
        self._resolver = resolver or LayeredResolver(self._guard)
        self._hub = hub or SubscriptionHub()

    # -- construction ----------------------------------------------------- #

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        registry: FlagRegistry | None = None,
        store: OverrideStore | None = None,
    ) -> RuntimeConfigPlane:
        """Build a plane whose base layer is bound to the live ``settings``."""
        base = (registry or build_default_registry()).bind_settings(settings)
        return cls(base, store=store)

    # -- registry / introspection ---------------------------------------- #

    @property
    def registry(self) -> FlagRegistry:
        return self._registry

    @property
    def hub(self) -> SubscriptionHub:
        return self._hub

    def spec(self, key: str) -> FlagSpec:
        """The :class:`FlagSpec` for ``key`` (raises :class:`UnknownFlagError`)."""
        return self._registry.get(key)

    # -- read API (typed, total, never raises) --------------------------- #

    def get(
        self, key: str, context: FlagContext | None = None
    ) -> Resolution:
        """Resolve ``key`` for ``context`` into a typed :class:`Resolution`.

        An unknown key yields a :class:`Resolution` with
        :attr:`ResolutionSource.UNKNOWN_FLAG` and a ``None`` value rather than
        raising — the read path is total.
        """
        spec = self._registry.try_get(key)
        if spec is None:
            return Resolution(
                key=key, value=None, source=ResolutionSource.UNKNOWN_FLAG, raw_value=None
            )
        layer = self._store.load()
        return self._resolver.resolve(spec, layer, context or EMPTY_CONTEXT)

    def value(
        self, key: str, context: FlagContext | None = None, *, default: FlagValue = None
    ) -> FlagValue:
        """The resolved raw value for ``key`` (``default`` for an unknown key)."""
        resolution = self.get(key, context)
        if resolution.source is ResolutionSource.UNKNOWN_FLAG:
            return default
        return resolution.value

    def is_enabled(self, key: str, context: FlagContext | None = None) -> bool:
        """Resolve a BOOL flag to ``True``/``False`` (``False`` for unknown/non-bool)."""
        value = self.value(key, context, default=False)
        return value is True

    def get_int(
        self, key: str, context: FlagContext | None = None, *, default: int = 0
    ) -> int:
        value = self.value(key, context, default=default)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    def get_float(
        self, key: str, context: FlagContext | None = None, *, default: float = 0.0
    ) -> float:
        value = self.value(key, context, default=default)
        if isinstance(value, bool):
            return default
        return float(value) if isinstance(value, int | float) else default

    def get_string(
        self, key: str, context: FlagContext | None = None, *, default: str = ""
    ) -> str:
        value = self.value(key, context, default=default)
        return value if isinstance(value, str) else default

    def get_json(
        self,
        key: str,
        context: FlagContext | None = None,
        *,
        default: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        value = self.value(key, context, default=default)
        return value if isinstance(value, dict | list) else default

    # -- subscription ---------------------------------------------------- #

    def subscribe(self, callback: Callable[[ChangeEvent], None]) -> Callable[[], None]:
        """Register a change callback (see :class:`SubscriptionHub`)."""
        return self._hub.subscribe(callback)

    # -- write API (validated, audited, notified) ------------------------ #

    def set_override(self, key: str, value: Any, *, actor: str | None = None) -> Resolution:
        """Set a global static override for ``key`` (validated + audited)."""
        spec = self._registry.get(key)
        coerced = self._validate_value(spec, value)
        before = self._overlay_dict(key)
        layer = self._store.load().set_static(key, coerced)
        self._commit(
            layer,
            key=key,
            kind=ChangeKind.SET_STATIC,
            actor=actor,
            before=before,
        )
        return self.get(key)

    def clear_override(self, key: str, *, actor: str | None = None) -> None:
        """Remove the global static override for ``key`` (audited)."""
        self._registry.get(key)  # validate the key exists
        before = self._overlay_dict(key)
        layer = self._store.load().clear_static(key)
        self._commit(
            layer, key=key, kind=ChangeKind.CLEAR_STATIC, actor=actor, before=before
        )

    def add_rule(
        self,
        key: str,
        rule: TargetingRule,
        *,
        actor: str | None = None,
    ) -> Resolution:
        """Add/replace a targeting rule for ``key`` (validated + audited)."""
        spec = self._registry.get(key)
        validated = TargetingRule(
            id=rule.id,
            value=self._validate_value(spec, rule.value),
            book=rule.book,
            user=rule.user,
            cohort=rule.cohort,
            provider=rule.provider,
            priority=rule.priority,
            rollout=rule.rollout,
            description=rule.description,
        )
        before = self._overlay_dict(key)
        layer = self._store.load().add_rule(key, validated)
        self._commit(layer, key=key, kind=ChangeKind.ADD_RULE, actor=actor, before=before)
        return self.get(key)

    def remove_rule(self, key: str, rule_id: str, *, actor: str | None = None) -> None:
        """Remove targeting rule ``rule_id`` from ``key`` (audited)."""
        self._registry.get(key)
        before = self._overlay_dict(key)
        layer = self._store.load().remove_rule(key, rule_id)
        self._commit(
            layer, key=key, kind=ChangeKind.REMOVE_RULE, actor=actor, before=before
        )

    def set_rollout(
        self,
        key: str,
        percent: float,
        *,
        bucket_by: str = "user",
        seed: int = 0,
        actor: str | None = None,
    ) -> Resolution:
        """Set a percentage rollout for ``key`` (validated + audited).

        The kill-switch guard still applies on resolution, so ramping a guarded
        flag never lifts it (a rollout cannot raise ``kinora.live_video``); we
        also reject configuring a rollout *on* a guarded kill-switch outright, as
        a ramp's intent is always "turn more on".
        """
        spec = self._registry.get(key)
        if spec.kill_switch:
            # A rollout's purpose is to raise reach — refuse it on a kill-switch.
            self._guard.check(spec, spec.default, self._resolver._rollout_on_value(spec))
        rollout = PercentRollout(
            flag_key=key,
            percent=max(0.0, min(100.0, percent)),
            bucket_by=bucket_by,
            seed=seed,
        )
        before = self._overlay_dict(key)
        layer = self._store.load().set_rollout(key, rollout)
        self._commit(
            layer, key=key, kind=ChangeKind.SET_ROLLOUT, actor=actor, before=before
        )
        return self.get(key)

    def clear_rollout(self, key: str, *, actor: str | None = None) -> None:
        """Remove the percentage rollout for ``key`` (audited)."""
        self._registry.get(key)
        before = self._overlay_dict(key)
        layer = self._store.load().set_rollout(key, None)
        self._commit(
            layer, key=key, kind=ChangeKind.CLEAR_ROLLOUT, actor=actor, before=before
        )

    def clear_flag(self, key: str, *, actor: str | None = None) -> None:
        """Revert ``key`` fully to its base (drop all overlays; audited)."""
        self._registry.get(key)
        before = self._overlay_dict(key)
        layer = self._store.load().clear_flag(key)
        self._commit(
            layer, key=key, kind=ChangeKind.CLEAR_FLAG, actor=actor, before=before
        )

    # -- snapshot / export / hot-reload ---------------------------------- #

    def export_overrides(self) -> dict[str, Any]:
        """The current override layer as a round-trippable dict (for backup/export)."""
        return self._store.load().to_dict()

    def import_overrides(
        self, data: dict[str, Any], *, actor: str | None = None
    ) -> None:
        """Replace the whole override layer (hot-reload), validating every value.

        Each overlaid value is coerced + guarded against its spec, so importing a
        layer that would raise a kill-switch is rejected before anything is
        persisted (all-or-nothing). Notifies subscribers with a single
        :class:`ChangeKind.RELOAD` event.
        """
        candidate = OverrideLayer.from_dict(data)
        self._validate_layer(candidate)
        # Re-version on top of the live layer so the monotone counter never goes
        # backwards (subscribers / caches rely on it strictly increasing).
        current_version = self._store.load().version
        relayered = OverrideLayer(
            overlays=candidate.overlays, version=current_version + 1
        )
        record = build_record(
            flag_key=None,
            kind=ChangeKind.RELOAD,
            actor=actor,
            before=self._store.load().to_dict(),
            after=relayered.to_dict(),
            layer_version=relayered.version,
        )
        self._store.save(relayered, record)
        self._hub.publish(
            ChangeEvent(
                kind=ChangeKind.RELOAD,
                flag_key=None,
                version=relayered.version,
                actor=actor,
                summary=record.summary,
            )
        )

    def snapshot(self, context: FlagContext | None = None) -> dict[str, Any]:
        """Export the *effective* configuration for ``context`` across all flags.

        The single call the desktop renderer / a worker makes to learn the full
        resolved state: every registered flag's resolved value + source for the
        given context, plus the spec catalog and override-layer version.
        """
        ctx = context or EMPTY_CONTEXT
        return {
            "layer_version": self._store.load().version,
            "context": ctx.to_dict(),
            "flags": {
                spec.key: self.get(spec.key, ctx).to_dict()
                for spec in self._registry.specs()
            },
        }

    def history(
        self, *, flag_key: str | None = None, limit: int = 50
    ) -> list[PlaneAuditRecord]:
        """Recent audit records (newest first), optionally filtered by flag key."""
        return self._store.history(flag_key=flag_key, limit=limit)

    # -- internals ------------------------------------------------------- #

    def _validate_value(self, spec: FlagSpec, value: Any) -> FlagValue:
        """Coerce ``value`` to the flag's type and reject a kill-switch raise."""
        coerced = spec.coerce(value)
        # Guard against the *base* (Settings) value — an override can never lift a
        # guarded flag above what Settings allows.
        self._guard.check(spec, spec.default, coerced)
        return coerced

    def _validate_layer(self, layer: OverrideLayer) -> None:
        """Validate every value in ``layer`` against its spec (raises on a bad one)."""
        for key, overlay in layer.overlays.items():
            spec = self._registry.get(key)
            if overlay.static is not None:
                self._validate_value(spec, overlay.static.value)
            for rule in overlay.rules:
                self._validate_value(spec, rule.value)
            if overlay.rollout is not None and spec.kill_switch:
                self._guard.check(
                    spec, spec.default, self._resolver._rollout_on_value(spec)
                )

    def _overlay_dict(self, key: str) -> dict[str, Any]:
        """The current overlay for ``key`` as a dict (for audit before/after)."""
        return self._store.load().overlay_for(key).to_dict()

    def _commit(
        self,
        layer: OverrideLayer,
        *,
        key: str,
        kind: ChangeKind,
        actor: str | None,
        before: dict[str, Any],
    ) -> None:
        """Persist ``layer`` with an audit record and notify subscribers."""
        after = layer.overlay_for(key).to_dict()
        record = build_record(
            flag_key=key,
            kind=kind,
            actor=actor,
            before=before,
            after=after,
            layer_version=layer.version,
        )
        self._store.save(layer, record)
        self._hub.publish(
            ChangeEvent(
                kind=kind,
                flag_key=key,
                version=layer.version,
                actor=actor,
                summary=record.summary,
            )
        )


__all__ = ["RuntimeConfigPlane"]
