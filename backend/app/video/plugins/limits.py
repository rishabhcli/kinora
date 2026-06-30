"""Resource budgets for a sandboxed plugin call.

A plugin declares the budget it *wants*; the host clamps that request against a
hard ceiling (:data:`HOST_CEILING`) so a manifest can never ask for more than
the host is willing to give. The clamped budget is what the sandbox enforces:

* ``wall_time_ms`` — the wall-clock deadline for one ``generate``/``probe`` call;
  exceeding it raises :class:`~app.video.plugins.errors.ResourceLimitError`.
* ``max_host_calls`` — how many capability-bearing host calls one invocation may
  make (a runaway plugin hammering the host is contained).
* ``max_output_bytes`` — the JSON-serialized size ceiling on a plugin's return
  value (a plugin cannot exhaust host memory by returning a giant blob).

These are deliberately small, total, side-effect-free numbers so the sandbox
tests are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.video.plugins.errors import ManifestError

#: Field defaults (kept as module constants so ``from_dict`` can reference them
#: without touching ``__slots__``-managed instance attributes on the dataclass).
_DEFAULT_WALL_TIME_MS = 30_000
_DEFAULT_MAX_HOST_CALLS = 64
_DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """A clamped-on-construction resource budget for one sandboxed call."""

    #: Wall-clock deadline for a single sandboxed call, in milliseconds.
    wall_time_ms: int = _DEFAULT_WALL_TIME_MS
    #: Maximum capability-bearing host calls per invocation.
    max_host_calls: int = _DEFAULT_MAX_HOST_CALLS
    #: Maximum JSON-serialized size of a plugin's return value.
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES

    @classmethod
    def from_dict(cls, data: Any) -> ResourceLimits:
        """Build limits from a manifest's ``resource_limits`` object (or use defaults)."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ManifestError("resource_limits must be an object")
        try:
            requested = cls(
                wall_time_ms=int(data.get("wall_time_ms", _DEFAULT_WALL_TIME_MS)),
                max_host_calls=int(data.get("max_host_calls", _DEFAULT_MAX_HOST_CALLS)),
                max_output_bytes=int(data.get("max_output_bytes", _DEFAULT_MAX_OUTPUT_BYTES)),
            )
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"resource_limits has a non-integer value: {exc}") from exc
        if requested.wall_time_ms <= 0 or requested.max_host_calls < 0:
            raise ManifestError("resource_limits values must be positive")
        if requested.max_output_bytes <= 0:
            raise ManifestError("resource_limits.max_output_bytes must be positive")
        return requested.clamped()

    def clamped(self, ceiling: ResourceLimits | None = None) -> ResourceLimits:
        """Return a copy with every field clamped to the host ceiling."""
        c = ceiling or HOST_CEILING
        return ResourceLimits(
            wall_time_ms=min(self.wall_time_ms, c.wall_time_ms),
            max_host_calls=min(self.max_host_calls, c.max_host_calls),
            max_output_bytes=min(self.max_output_bytes, c.max_output_bytes),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "wall_time_ms": self.wall_time_ms,
            "max_host_calls": self.max_host_calls,
            "max_output_bytes": self.max_output_bytes,
        }


#: The hard host ceiling — no plugin budget may exceed these, ever.
HOST_CEILING = ResourceLimits(
    wall_time_ms=120_000,
    max_host_calls=256,
    max_output_bytes=8_000_000,
)


__all__ = ["HOST_CEILING", "ResourceLimits"]
