"""The typed hook registry + deterministic dispatcher.

The registry is the in-memory index of *which loaded plugin hooks attach to
which extension point*, and the dispatcher is the engine that runs them through
the sandbox in a defined order, composing their results per :class:`HookKind`:

* ``TRANSFORM`` (ingest filters) — fold: each hook receives the running payload
  and returns a replacement; the final payload is returned. A hook that returns
  ``None`` is treated as a no-op pass-through (keeps the prior payload).
* ``PRODUCE`` (custom agents, render post-processors) — map: each hook's value
  is collected into an ordered list of :class:`HookOutcome` (value + metering).
* ``OBSERVE`` (webhook actions) — for-effect: values are discarded; only success
  / failure is recorded.

Determinism: hooks run in ``(priority, plugin_id, hook_id)`` order — never
registration order — so the same set always composes the same way. A hook that
raises a :class:`SandboxError` is *isolated*: by default the dispatcher records
the failure and continues with the remaining hooks (one bad plugin cannot break
the pipeline), and the running payload is preserved for ``TRANSFORM``. Strict
mode (``fail_fast``) re-raises instead, for tests and trusted pipelines.

The registry binds each hook to its plugin's :class:`GrantSet`,
:class:`ResourceLimits`, and the per-plugin :class:`HostServices` — so two
plugins at the same point are sandboxed independently with their own authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import HookError, SandboxError
from app.platform.plugins.hooks import EXTENSION_POINT_KIND, ExtensionPoint, HookKind, HookSpec
from app.platform.plugins.limits import ResourceLimits
from app.platform.plugins.runtime import InvocationResult, LoadedPlugin, PluginRuntime


@dataclass(frozen=True, slots=True)
class RegisteredHook:
    """A hook bound to its plugin instance + sandbox context, ready to dispatch."""

    plugin_id: str
    version: str
    spec: HookSpec
    plugin: LoadedPlugin
    grants: GrantSet
    limits: ResourceLimits
    services: HostServices

    @property
    def point(self) -> ExtensionPoint:
        return self.spec.point

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Deterministic order: priority, then plugin id, then hook id."""
        return (self.spec.priority, self.plugin_id, self.spec.id)


@dataclass(slots=True)
class HookOutcome:
    """The result of one hook invocation (success or isolated failure)."""

    plugin_id: str
    hook_id: str
    point: ExtensionPoint
    ok: bool
    value: Any = None
    error_code: str | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    host_calls: int = 0
    capabilities_used: tuple[str, ...] = ()
    wall_time_ms: float = 0.0

    @classmethod
    def success(cls, hook: RegisteredHook, result: InvocationResult) -> HookOutcome:
        return cls(
            plugin_id=hook.plugin_id,
            hook_id=hook.spec.id,
            point=hook.point,
            ok=True,
            value=result.value,
            logs=result.logs,
            host_calls=result.host_calls,
            capabilities_used=result.capabilities_used,
            wall_time_ms=result.wall_time_ms,
        )

    @classmethod
    def failure(cls, hook: RegisteredHook, exc: Exception) -> HookOutcome:
        code = getattr(exc, "code", "plugin_error")
        return cls(
            plugin_id=hook.plugin_id,
            hook_id=hook.spec.id,
            point=hook.point,
            ok=False,
            error_code=code,
            error=str(exc),
        )


@dataclass(slots=True)
class DispatchReport:
    """The aggregate result of dispatching one extension point."""

    point: ExtensionPoint
    kind: HookKind
    outcomes: list[HookOutcome] = field(default_factory=list)
    #: For TRANSFORM points: the final folded payload.
    payload: Any = None

    @property
    def succeeded(self) -> list[HookOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[HookOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def values(self) -> list[Any]:
        """For PRODUCE points: the ordered list of successful hook values."""
        return [o.value for o in self.outcomes if o.ok]

    @property
    def all_ok(self) -> bool:
        return all(o.ok for o in self.outcomes)


class HookRegistry:
    """An in-memory, deterministic registry + dispatcher of sandboxed hooks."""

    def __init__(self, runtime: PluginRuntime | None = None) -> None:
        self._runtime = runtime or PluginRuntime()
        self._by_point: dict[ExtensionPoint, list[RegisteredHook]] = {
            point: [] for point in ExtensionPoint
        }
        self._plugins: set[str] = set()

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register(self, hook: RegisteredHook) -> None:
        """Register one bound hook. Idempotent per (plugin_id, hook_id, point)."""
        bucket = self._by_point[hook.point]
        key = (hook.plugin_id, hook.spec.id)
        if any((h.plugin_id, h.spec.id) == key for h in bucket):
            raise HookError(
                f"hook {hook.spec.id!r} of plugin {hook.plugin_id!r} already registered"
                f" at {hook.point.value}"
            )
        bucket.append(hook)
        bucket.sort(key=lambda h: h.sort_key)
        self._plugins.add(hook.plugin_id)

    def register_plugin(
        self,
        *,
        plugin: LoadedPlugin,
        hooks: tuple[HookSpec, ...],
        grants: GrantSet,
        limits: ResourceLimits,
        services: HostServices,
    ) -> int:
        """Bind + register every hook a plugin declares. Returns the count added."""
        count = 0
        for spec in hooks:
            self.register(
                RegisteredHook(
                    plugin_id=plugin.plugin_id,
                    version=plugin.version,
                    spec=spec,
                    plugin=plugin,
                    grants=grants,
                    limits=limits,
                    services=services,
                )
            )
            count += 1
        return count

    def unregister_plugin(self, plugin_id: str) -> int:
        """Remove every hook belonging to ``plugin_id``. Returns the count removed."""
        removed = 0
        for point, bucket in self._by_point.items():
            kept = [h for h in bucket if h.plugin_id != plugin_id]
            removed += len(bucket) - len(kept)
            self._by_point[point] = kept
        self._plugins.discard(plugin_id)
        return removed

    def hooks_at(self, point: ExtensionPoint) -> tuple[RegisteredHook, ...]:
        """The registered hooks at ``point`` in deterministic dispatch order."""
        return tuple(self._by_point[point])

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(self._plugins)

    def __len__(self) -> int:
        return sum(len(b) for b in self._by_point.values())

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def dispatch(
        self,
        point: ExtensionPoint,
        payload: Any,
        *,
        fail_fast: bool = False,
    ) -> DispatchReport:
        """Run every hook at ``point`` through the sandbox and compose results.

        ``payload`` is the input. For ``TRANSFORM`` points it is threaded
        through the hook chain; the final value lands in ``report.payload``. A
        failing hook is isolated (recorded, skipped) unless ``fail_fast``.
        """
        kind = EXTENSION_POINT_KIND[point]
        report = DispatchReport(point=point, kind=kind, payload=payload)
        running = payload

        for hook in self._by_point[point]:
            try:
                result = self._runtime.invoke(
                    hook.plugin,
                    hook.spec.entrypoint,
                    running if kind is HookKind.TRANSFORM else payload,
                    grants=hook.grants,
                    services=hook.services,
                    limits=hook.limits,
                )
            except SandboxError as exc:
                if fail_fast:
                    raise
                report.outcomes.append(HookOutcome.failure(hook, exc))
                continue
            except Exception as exc:  # noqa: BLE001 - any unexpected dispatch error
                if fail_fast:
                    raise
                report.outcomes.append(HookOutcome.failure(hook, exc))
                continue

            outcome = HookOutcome.success(hook, result)
            report.outcomes.append(outcome)
            if kind is HookKind.TRANSFORM and result.value is not None:
                running = result.value

        if kind is HookKind.TRANSFORM:
            report.payload = running
        return report


__all__ = [
    "DispatchReport",
    "HookOutcome",
    "HookRegistry",
    "RegisteredHook",
]
