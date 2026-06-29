"""Resource limits — the sandbox's quantitative budget.

A :class:`ResourceLimits` bundle is the *ceiling* the host enforces on a plugin
invocation: how long it may run, how much memory it may allocate, how many host
API calls it may make, and how much output it may return. A plugin's manifest
*requests* limits; the host clamps them to operator-configured maxima
(:meth:`ResourceLimits.clamp_to`) so a manifest can never widen its own budget.

These are pure value objects with validation; enforcement lives in the runtime
(:mod:`app.platform.plugins.runtime`) and the broker. Keeping the policy
declarative here means the deterministic sandbox tests assert on the *limits*
that tripped, not on wall-clock flakiness.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.platform.plugins.errors import PluginValidationError

#: Conservative defaults: a hook should be cheap. Operators raise these per
#: trust tier via the host-configured maxima.
DEFAULT_WALL_TIME_MS = 2_000
DEFAULT_CPU_TIME_MS = 1_000
DEFAULT_MEMORY_BYTES = 64 * 1024 * 1024  # 64 MiB
DEFAULT_MAX_HOST_CALLS = 64
DEFAULT_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_MAX_LOG_LINES = 100


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """The per-invocation resource ceiling for a sandboxed plugin call."""

    wall_time_ms: int = DEFAULT_WALL_TIME_MS
    cpu_time_ms: int = DEFAULT_CPU_TIME_MS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    max_host_calls: int = DEFAULT_MAX_HOST_CALLS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_log_lines: int = DEFAULT_MAX_LOG_LINES

    def __post_init__(self) -> None:
        for field, value in (
            ("wall_time_ms", self.wall_time_ms),
            ("cpu_time_ms", self.cpu_time_ms),
            ("memory_bytes", self.memory_bytes),
            ("max_host_calls", self.max_host_calls),
            ("max_output_bytes", self.max_output_bytes),
            ("max_log_lines", self.max_log_lines),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PluginValidationError(
                    f"resource limit {field} must be a positive int, got {value!r}"
                )

    def clamp_to(self, ceiling: ResourceLimits) -> ResourceLimits:
        """Return a copy where every field is min(self, ceiling).

        This is how the host guarantees a manifest cannot *request* a budget
        wider than the operator allows — the effective limit is always the
        tighter of (requested, ceiling).
        """
        return ResourceLimits(
            wall_time_ms=min(self.wall_time_ms, ceiling.wall_time_ms),
            cpu_time_ms=min(self.cpu_time_ms, ceiling.cpu_time_ms),
            memory_bytes=min(self.memory_bytes, ceiling.memory_bytes),
            max_host_calls=min(self.max_host_calls, ceiling.max_host_calls),
            max_output_bytes=min(self.max_output_bytes, ceiling.max_output_bytes),
            max_log_lines=min(self.max_log_lines, ceiling.max_log_lines),
        )

    def with_(self, **changes: int) -> ResourceLimits:
        """A copy with selected fields overridden (validated by ``__post_init__``)."""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, int]:
        return {
            "wall_time_ms": self.wall_time_ms,
            "cpu_time_ms": self.cpu_time_ms,
            "memory_bytes": self.memory_bytes,
            "max_host_calls": self.max_host_calls,
            "max_output_bytes": self.max_output_bytes,
            "max_log_lines": self.max_log_lines,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> ResourceLimits:
        """Build from a (partial) dict; absent fields fall back to defaults."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise PluginValidationError("resource limits must be an object")
        known = {
            "wall_time_ms",
            "cpu_time_ms",
            "memory_bytes",
            "max_host_calls",
            "max_output_bytes",
            "max_log_lines",
        }
        unknown = set(data) - known
        if unknown:
            raise PluginValidationError(f"unknown resource-limit fields: {sorted(unknown)}")
        coerced: dict[str, int] = {}
        for key, value in data.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise PluginValidationError(f"resource limit {key} must be an int")
            coerced[key] = value
        return cls(**coerced)


#: The default operator ceiling: equal to the conservative defaults. Operators
#: raise this per trust tier; a manifest is clamped against the effective value.
DEFAULT_CEILING = ResourceLimits()


__all__ = [
    "DEFAULT_CEILING",
    "DEFAULT_CPU_TIME_MS",
    "DEFAULT_MAX_HOST_CALLS",
    "DEFAULT_MAX_LOG_LINES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MEMORY_BYTES",
    "DEFAULT_WALL_TIME_MS",
    "ResourceLimits",
]
