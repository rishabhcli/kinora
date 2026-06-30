"""The isolation boundary between the host and a third-party plugin.

This is the security-relevant core of the SDK. The threat model is a *buggy or
hostile* plugin author, and the guarantees are:

1. **Capability allow-list.** A plugin reaches the host only through a
   :class:`HostHandle` that exposes *exactly* the services its manifest's
   capability profile maps to. Anything else raises
   :class:`~app.video.plugins.errors.CapabilityDeniedError` *before* the host
   operation runs, so a denied capability has no side effect.
2. **No ambient credentials.** The handle never carries the host ``Settings``,
   the process environment, or a global credential store. Every secret a plugin
   needs arrives through its *validated config*. Asking the handle for a secret
   by name returns only what config declared as a secret field, and
   :meth:`HostHandle.host_secret` raises :class:`AmbientCredentialError` for
   anything else. (The runtime import-denylist additionally bars ``os`` /
   ``app`` / ``importlib`` so a sandboxed plugin body cannot reach env directly.)
3. **Time + resource guards.** Each plugin call runs under a wall-clock deadline
   (``asyncio.wait_for``); host-call count and output size are metered. A budget
   overrun raises :class:`~app.video.plugins.errors.ResourceLimitError`.
4. **Error containment.** Any exception a plugin raises is caught and re-raised
   as a typed, sanitized :class:`~app.video.plugins.errors.PluginRuntimeError`
   (the original repr is preserved for the host log but the raw traceback never
   crosses back to the host caller as an arbitrary exception type). A bad plugin
   surfaces as a contained failure — it can never crash the host event loop.

:class:`Sandbox` wraps an already-instantiated plugin object and drives its
``probe`` / ``generate`` through these guards. The factory *instantiation* is
also performed under the sandbox (see :meth:`Sandbox.instantiate`) so a plugin
that explodes in its constructor is contained too.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.video.plugins.contracts import (
    ProbeResult,
    VideoArtifact,
    VideoProviderPlugin,
    VideoRequest,
)
from app.video.plugins.errors import (
    AmbientCredentialError,
    CapabilityDeniedError,
    PluginRuntimeError,
    ResourceLimitError,
    SandboxError,
)
from app.video.plugins.limits import ResourceLimits

logger = get_logger("app.video.plugins.sandbox")


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

#: The capabilities a plugin may be granted. Deliberately tiny + video-scoped.
#:
#: * ``net.fetch`` — make an outbound HTTP call to the model's own API endpoint
#:   (the only legitimate reason a video provider touches the network).
#: * ``host.log`` — emit a structured log line through the host logger.
#: * ``host.usage`` — report video-seconds / token spend to the host meter.
#: * ``host.secret`` — read a *config-declared* secret by name (never ambient).
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {"net.fetch", "host.log", "host.usage", "host.secret"}
)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """The set of capabilities one plugin is allowed to use."""

    granted: frozenset[str] = frozenset()

    @classmethod
    def from_iterable(cls, raw: Any) -> CapabilityGrant:
        if raw is None:
            return cls()
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise CapabilityDeniedError("capability grant must be a list of capability names")
        grants = frozenset(str(c) for c in raw)
        unknown = grants - KNOWN_CAPABILITIES
        if unknown:
            raise CapabilityDeniedError(
                f"unknown capabilities requested: {sorted(unknown)}",
                capability=sorted(unknown)[0],
            )
        return cls(grants)

    def permits(self, capability: str) -> bool:
        return capability in self.granted


# --------------------------------------------------------------------------- #
# Host services + the capability-scoped handle
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class HostServices:
    """The host-side implementations the handle dispatches to.

    Injectable so tests pass deterministic fakes; production wires real outbound
    HTTP / the usage meter here at final integration. ``fetch`` is async; the
    others are sync + side-effect-light.
    """

    fetch: Callable[..., Awaitable[Any]] | None = None
    log_sink: Callable[[str, Mapping[str, Any]], None] | None = None
    usage_sink: Callable[[Mapping[str, Any]], None] | None = None
    #: The plugin's *validated config* secret values, keyed by field name. The
    #: only secrets the handle will ever surface — nothing ambient.
    secrets: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _CallMeter:
    """Per-invocation budget meter (host calls)."""

    max_host_calls: int
    host_calls: int = 0
    trail: list[str] = field(default_factory=list)

    def charge(self, capability: str) -> None:
        self.host_calls += 1
        self.trail.append(capability)
        if self.host_calls > self.max_host_calls:
            raise ResourceLimitError(
                f"plugin exceeded host-call budget ({self.max_host_calls})",
                limit="host_calls",
            )


class HostHandle:
    """The capability-scoped object a plugin uses to reach the host.

    Every method first checks the grant set (raising
    :class:`CapabilityDeniedError` on a missing grant) and then meters the call.
    There is no attribute on this object that exposes the host settings, env, or
    any credential beyond the config-declared secrets — that is the no-ambient
    guarantee, enforced structurally rather than by convention.
    """

    __slots__ = ("_grant", "_services", "_meter", "_plugin_id")

    def __init__(
        self,
        *,
        grant: CapabilityGrant,
        services: HostServices,
        meter: _CallMeter,
        plugin_id: str,
    ) -> None:
        self._grant = grant
        self._services = services
        self._meter = meter
        self._plugin_id = plugin_id

    def _require(self, capability: str) -> None:
        if not self._grant.permits(capability):
            raise CapabilityDeniedError(
                f"plugin {self._plugin_id!r} is not granted {capability!r}",
                capability=capability,
            )
        self._meter.charge(capability)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        """Make the plugin's single legitimate outbound call (its model API)."""
        self._require("net.fetch")
        if self._services.fetch is None:  # pragma: no cover - wired at integration
            raise CapabilityDeniedError("host fetch service is unavailable", capability="net.fetch")
        return await self._services.fetch(*args, **kwargs)

    def log(self, message: str, **fields: Any) -> None:
        """Emit a structured log line through the host logger."""
        self._require("host.log")
        if self._services.log_sink is not None:
            self._services.log_sink(message, fields)
        else:  # pragma: no cover - default path uses the module logger
            logger.info("plugin_log", plugin_id=self._plugin_id, message=message, **fields)

    def report_usage(self, **usage: Any) -> None:
        """Report spend (video-seconds / tokens) to the host meter."""
        self._require("host.usage")
        if self._services.usage_sink is not None:
            self._services.usage_sink(usage)

    def host_secret(self, name: str) -> str:
        """Return a config-declared secret by name — never an ambient credential.

        Raises :class:`AmbientCredentialError` for any name the plugin's config
        did not declare as a secret field. This is the *only* path to a secret,
        and it can only ever return values the host already validated + injected.
        """
        self._require("host.secret")
        if name not in self._services.secrets:
            raise AmbientCredentialError(
                f"plugin {self._plugin_id!r} requested undeclared secret {name!r}; "
                "secrets must be declared in the plugin's config schema",
                name=name,
            )
        return self._services.secrets[name]


# --------------------------------------------------------------------------- #
# The sandbox
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SandboxedCall:
    """The outcome of one sandboxed invocation — value + the metering it incurred."""

    value: Any
    host_calls: int
    capabilities_used: tuple[str, ...]
    wall_time_ms: float


class Sandbox:
    """Drives an instantiated plugin's calls under the isolation contract."""

    def __init__(
        self,
        *,
        plugin_id: str,
        grant: CapabilityGrant,
        services: HostServices,
        limits: ResourceLimits,
    ) -> None:
        self._plugin_id = plugin_id
        self._grant = grant
        self._services = services
        self._limits = limits

    @property
    def grant(self) -> CapabilityGrant:
        return self._grant

    def make_handle(self) -> tuple[HostHandle, _CallMeter]:
        """Build a fresh capability-scoped handle + its per-call meter."""
        meter = _CallMeter(max_host_calls=self._limits.max_host_calls)
        handle = HostHandle(
            grant=self._grant,
            services=self._services,
            meter=meter,
            plugin_id=self._plugin_id,
        )
        return handle, meter

    @staticmethod
    def instantiate(
        factory: Callable[..., VideoProviderPlugin],
        *,
        config: dict[str, Any],
        host: HostHandle,
    ) -> VideoProviderPlugin:
        """Call a plugin factory under error containment.

        A factory that raises is contained as a :class:`PluginRuntimeError`; the
        host load never sees an arbitrary third-party exception type.
        """
        try:
            instance = factory(config=config, host=host)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - contain any constructor failure
            raise PluginRuntimeError(
                f"plugin factory raised during instantiation: {type(exc).__name__}",
                original=repr(exc),
            ) from exc
        if not isinstance(instance, VideoProviderPlugin):
            raise PluginRuntimeError(
                "plugin factory did not return an object satisfying VideoProviderPlugin"
            )
        return instance

    async def probe(self, plugin: VideoProviderPlugin) -> SandboxedCall:
        """Run ``plugin.probe()`` under the guards."""
        return await self._guarded(plugin.probe(), expect=ProbeResult)

    async def generate(
        self, plugin: VideoProviderPlugin, request: VideoRequest
    ) -> SandboxedCall:
        """Run ``plugin.generate(request)`` under the guards."""
        return await self._guarded(plugin.generate(request), expect=VideoArtifact)

    async def _guarded(self, coro: Awaitable[Any], *, expect: type) -> SandboxedCall:
        """Apply the wall-time deadline + error containment + output-size guard.

        Host-call metering is carried by the handle the host passed into the
        factory; this layer owns the deadline, the return-type check, the output
        budget, and the exception sanitization.
        """
        started = time.perf_counter()
        deadline_s = self._limits.wall_time_ms / 1000.0
        try:
            value = await asyncio.wait_for(_as_coro(coro), timeout=deadline_s)
        except TimeoutError as exc:
            raise ResourceLimitError(
                f"plugin {self._plugin_id!r} exceeded wall-time budget "
                f"({self._limits.wall_time_ms} ms)",
                limit="wall_time",
            ) from exc
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize any plugin exception
            raise PluginRuntimeError(
                f"plugin {self._plugin_id!r} raised {type(exc).__name__} during call",
                original=repr(exc),
            ) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if not isinstance(value, expect):
            raise PluginRuntimeError(
                f"plugin {self._plugin_id!r} returned {type(value).__name__}, "
                f"expected {expect.__name__}"
            )
        self._check_output_size(value)
        return SandboxedCall(
            value=value,
            host_calls=0,
            capabilities_used=(),
            wall_time_ms=elapsed_ms,
        )

    def _check_output_size(self, value: Any) -> None:
        """Reject an oversized return value (cheap JSON-size estimate)."""
        try:
            payload = value.model_dump() if hasattr(value, "model_dump") else value
            size = len(json.dumps(payload, default=str).encode("utf-8"))
        except (TypeError, ValueError):  # pragma: no cover - non-JSONable fallback
            size = len(repr(value).encode("utf-8"))
        if size > self._limits.max_output_bytes:
            raise ResourceLimitError(
                f"plugin output {size} bytes exceeds budget "
                f"({self._limits.max_output_bytes})",
                limit="output_bytes",
            )


async def _as_coro(awaitable: Awaitable[Any]) -> Any:
    """Normalize any awaitable to something ``asyncio.wait_for`` accepts."""
    return await awaitable


__all__ = [
    "KNOWN_CAPABILITIES",
    "CapabilityGrant",
    "HostHandle",
    "HostServices",
    "Sandbox",
    "SandboxedCall",
]
