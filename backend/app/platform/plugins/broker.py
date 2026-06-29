"""The host-API broker — the only door from sandboxed code to host power.

Sandboxed plugin code has **no ambient authority**: no filesystem, no sockets,
no host imports. The *single* object it can use to affect the outside world is a
:class:`HostAPI` handle the runtime injects as ``host``. Every method on it is
capability-gated: the broker checks the plugin's :class:`GrantSet` *before* it
calls the underlying host function, so a denied capability never produces a side
effect — it raises :class:`CapabilityDeniedError`.

The broker also meters: each brokered call decrements the invocation's
``max_host_calls`` budget and each ``host.log`` line decrements ``max_log_lines``.
Exhausting either raises :class:`ResourceLimitError`. This makes the security
contract *testable without infrastructure* — the deterministic tests assert that
an ungranted call raises before any backend is touched, using in-memory host
implementations.

The actual host functionality is supplied as a :class:`HostServices` bundle of
plain callables (sync or async). In production these wrap the real canon / KV /
HTTP services; in tests they are in-memory fakes. The broker doesn't care which
— it only enforces the capability + budget contract around them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.platform.plugins.capabilities import GrantSet
from app.platform.plugins.errors import CapabilityDeniedError, ResourceLimitError

#: A host service is any callable returning a value or an awaitable.
HostCallable = Callable[..., Any]


@dataclass(slots=True)
class CallMeter:
    """Mutable per-invocation counters the broker decrements on each use."""

    max_host_calls: int
    max_log_lines: int
    host_calls: int = 0
    log_lines: int = 0
    #: An ordered audit trail of every brokered capability touch (granted ones).
    trail: list[str] = field(default_factory=list)

    def charge_call(self, capability: str) -> None:
        self.host_calls += 1
        if self.host_calls > self.max_host_calls:
            raise ResourceLimitError(
                f"exceeded host-call budget ({self.max_host_calls})",
                limit="host_calls",
            )
        self.trail.append(capability)

    def charge_log(self) -> None:
        self.log_lines += 1
        if self.log_lines > self.max_log_lines:
            raise ResourceLimitError(
                f"exceeded log-line budget ({self.max_log_lines})",
                limit="log_lines",
            )


@dataclass(frozen=True, slots=True)
class HostServices:
    """The concrete host functions the broker fronts (DI seam).

    Each entry is keyed by capability scope. A service may be *absent* (None):
    the broker then raises a clear error explaining the host does not implement
    that capability here, distinct from "you weren't granted it". Both block the
    call; the distinction is for operability.
    """

    services: Mapping[str, HostCallable] = field(default_factory=dict)

    def get(self, capability: str) -> HostCallable | None:
        return self.services.get(capability)


class HostAPI:
    """The capability-gated handle injected into sandboxed code as ``host``.

    Plugin code calls e.g. ``host.call('canon.query', beat_id=...)`` or the
    convenience wrappers (``host.log(...)``, ``host.kv_get(...)``). Every path
    funnels through :meth:`call`, which enforces the grant + budget contract.
    """

    def __init__(
        self,
        *,
        grants: GrantSet,
        services: HostServices,
        meter: CallMeter,
        logs: list[str],
    ) -> None:
        self._grants = grants
        self._services = services
        self._meter = meter
        self._logs = logs

    # -- the single gated entrypoint ----------------------------------- #

    def call(self, capability: str, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a host service after checking the grant + charging the budget.

        Raises :class:`CapabilityDeniedError` *before* touching the service if
        the plugin lacks the scope; raises :class:`ResourceLimitError` if the
        host-call budget is exhausted. A coroutine returned by an async service
        is surfaced to the caller as-is (the runtime awaits it).
        """
        # 1. Authority check — deny by default. This MUST precede any side effect.
        self._grants.require(capability)
        # 2. Budget check + audit.
        self._meter.charge_call(capability)
        # 3. Resolve + invoke the underlying host function.
        fn = self._services.get(capability)
        if fn is None:
            raise CapabilityDeniedError(
                f"host does not provide capability {capability!r} in this context",
                capability=capability,
            )
        return fn(*args, **kwargs)

    def permits(self, capability: str) -> bool:
        """Non-charging predicate so well-behaved plugins can branch on grants."""
        return self._grants.permits(capability)

    # -- ergonomic wrappers (still gated via .call) -------------------- #

    def log(self, message: str, /, **fields: Any) -> None:
        """Emit a structured log line (capability ``log.write``)."""
        self._grants.require("log.write")
        self._meter.charge_log()
        line = str(message)
        if fields:
            line += " " + " ".join(f"{k}={v!r}" for k, v in sorted(fields.items()))
        self._logs.append(line)

    def kv_get(self, key: str) -> Any:
        """Read the plugin's scoped key/value store (capability ``storage.kv.read``)."""
        return self.call("storage.kv.read", key)

    def kv_set(self, key: str, value: Any) -> Any:
        """Write the plugin's scoped key/value store (capability ``storage.kv.write``)."""
        return self.call("storage.kv.write", key, value)

    def canon_query(self, beat_id: str, **kwargs: Any) -> Any:
        """Run the §8.3 retrieval policy (capability ``canon.query``)."""
        return self.call("canon.query", beat_id, **kwargs)

    def fetch(self, url: str, **kwargs: Any) -> Any:
        """Host-mediated outbound HTTP (capability ``net.fetch``)."""
        return self.call("net.fetch", url, **kwargs)

    def secret(self, name: str) -> Any:
        """Read a named host-managed secret (capability ``secrets.read``)."""
        return self.call("secrets.read", name)


def make_async_host_call(
    services: HostServices,
) -> Callable[[str], HostCallable | None]:  # pragma: no cover - thin accessor
    """Return a resolver for async service callables (used by the async runtime)."""

    def _resolve(capability: str) -> HostCallable | None:
        return services.get(capability)

    return _resolve


async def maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, else return it (broker result helper)."""
    if isinstance(value, Awaitable):
        return await value
    return value


__all__ = [
    "CallMeter",
    "HostAPI",
    "HostCallable",
    "HostServices",
    "make_async_host_call",
    "maybe_await",
]
