"""The fault library — composable, per-dependency scoped disruptions.

This is the *orchestrated, scenario-level* fault layer. Unlike a per-call random
chaos injector (which decides "does this single call fail?" with a probability),
a :class:`Fault` here is a **named, scoped, arm/disarm-able disruption** that a
game-day arms for a whole window against one *dependency* (``dashscope``,
``redis``, ``postgres``, ``object_store``, …). The runner arms a set of faults,
watches a steady-state guard, and disarms them all on completion or breach.

Eight fault kinds (kinora.md §4.11 / §12.1 failure modes the system must absorb):

* :class:`LatencyFault` — add a fixed/jittered delay, then proceed.
* :class:`ErrorFault` — raise an injected exception instead of calling through.
* :class:`TimeoutFault` — sleep past a deadline, then raise a timeout error.
* :class:`ConnectionDropFault` — raise a connection-reset-style error (no delay).
* :class:`PartialResponseFault` — call through, then truncate/corrupt the result.
* :class:`ClockSkewFault` — report a skewed "now" to the call (no raise).
* :class:`DependencyDownFault` — hard-down: every call fails fast for the window.
* :class:`RateLimitStormFault` — after N calls in the window, 429 the remainder.

A fault decides its behaviour through a deterministic, **seeded** RNG and the
injected :class:`~app.chaos.clock.Clock`, so a game-day's exact disruption
sequence is reproducible. ``Fault.apply`` is the single seam the interceptor
calls; it returns a :class:`FaultEffect` describing what to do to the wrapped
call, and the interceptor (not the fault) performs the sleep/raise/mutate so the
faults stay pure and unit-testable.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FaultKind(StrEnum):
    """The catalogue of disruption kinds the library can inject."""

    LATENCY = "latency"
    ERROR = "error"
    TIMEOUT = "timeout"
    CONNECTION_DROP = "connection_drop"
    PARTIAL_RESPONSE = "partial_response"
    CLOCK_SKEW = "clock_skew"
    DEPENDENCY_DOWN = "dependency_down"
    RATE_LIMIT_STORM = "rate_limit_storm"


class InjectedFault(RuntimeError):  # noqa: N818 - it *is* a fault; the name is intentional.
    """The base exception an armed chaos fault raises.

    Carries the dependency it was scoped to and the fault name so a steady-state
    probe / findings report can attribute a failure to the exact injection.
    """

    def __init__(self, dependency: str, fault: str, message: str) -> None:
        super().__init__(message)
        self.dependency = dependency
        self.fault = fault


class InjectedTimeout(InjectedFault):
    """A timeout-style injected fault (a deadline was exceeded then surfaced)."""


class InjectedConnectionError(InjectedFault):
    """A connection-reset / dropped-socket style injected fault."""


class InjectedRateLimit(InjectedFault):
    """A 429-style rate-limit injected fault (carries an optional retry hint)."""

    def __init__(self, dependency: str, fault: str, message: str, retry_after_s: float) -> None:
        super().__init__(dependency, fault, message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class FaultEffect:
    """What the interceptor should do to one wrapped call.

    Exactly one disposition is meaningful per effect, but the fields compose:
    ``delay_s`` is applied first (a sleep), then if ``raises`` is set the
    interceptor raises it *instead* of calling through; otherwise it calls
    through and, if ``transform`` is set, passes the real result through it.
    ``skew_s`` is surfaced to callers that read a clock (clock-skew fault).
    """

    delay_s: float = 0.0
    raises: BaseException | None = None
    transform: Callable[[Any], Any] | None = None
    skew_s: float = 0.0
    #: Human label of what this effect did, for the timeline in the report.
    label: str = "passthrough"

    @property
    def is_passthrough(self) -> bool:
        """True when the call proceeds unchanged (no delay, raise, or mutate)."""
        return (
            self.delay_s == 0.0
            and self.raises is None
            and self.transform is None
            and self.skew_s == 0.0
        )


@dataclass(slots=True)
class FaultContext:
    """Per-call context handed to :meth:`Fault.apply`.

    ``call_index`` is the monotonically-increasing index of this call *within the
    dependency this fault is scoped to*, used by stateful faults (rate-limit
    storm). ``rng`` is the seeded RNG; ``now`` is the injected clock's current
    monotonic time so faults can reason about how long they have been armed.
    """

    dependency: str
    call_index: int
    rng: random.Random
    now: float
    #: Monotonic time the fault was armed (so windowed faults can measure age).
    armed_at: float


@dataclass(frozen=True, slots=True)
class Fault:
    """Base class for all faults. Subclasses override :meth:`compute`.

    A fault is *scoped* to one ``dependency`` (its blast-radius unit) and is
    deterministic given the RNG + clock in its :class:`FaultContext`. ``name``
    identifies it in reports; ``probability`` is the per-call chance the fault
    fires (``1.0`` = every call), checked here so subclasses only describe the
    disruption, not whether it triggers.
    """

    dependency: str
    name: str = "fault"
    probability: float = 1.0

    kind: FaultKind = field(default=FaultKind.ERROR, init=False)

    def apply(self, ctx: FaultContext) -> FaultEffect:
        """Decide this call's effect. Honours ``probability`` then delegates."""
        if self.probability < 1.0 and ctx.rng.random() >= self.probability:
            return FaultEffect()
        return self.compute(ctx)

    def compute(self, ctx: FaultContext) -> FaultEffect:  # pragma: no cover - overridden
        """Subclass hook: return the :class:`FaultEffect` for a triggered call."""
        raise NotImplementedError


def _error(dep: str, name: str, msg: str) -> InjectedFault:
    return InjectedFault(dep, name, msg)


@dataclass(frozen=True, slots=True)
class LatencyFault(Fault):
    """Add latency, then proceed. ``jitter_s`` adds ``U(0, jitter_s)`` per call."""

    base_latency_s: float = 1.0
    jitter_s: float = 0.0
    kind: FaultKind = field(default=FaultKind.LATENCY, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        jitter = ctx.rng.uniform(0.0, self.jitter_s) if self.jitter_s > 0 else 0.0
        delay = self.base_latency_s + jitter
        return FaultEffect(delay_s=delay, label=f"latency+{delay:.3f}s")


@dataclass(frozen=True, slots=True)
class ErrorFault(Fault):
    """Raise an injected error instead of calling through."""

    message: str = "injected error"
    exc_factory: Callable[[str, str, str], BaseException] | None = None
    kind: FaultKind = field(default=FaultKind.ERROR, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        factory = self.exc_factory or _error
        exc = factory(self.dependency, self.name, self.message)
        return FaultEffect(raises=exc, label="error")


@dataclass(frozen=True, slots=True)
class TimeoutFault(Fault):
    """Sleep past ``deadline_s`` then raise :class:`InjectedTimeout`.

    Models a hung dependency: the caller spends the deadline waiting before the
    timeout surfaces. The interceptor performs the (virtual) sleep.
    """

    deadline_s: float = 5.0
    kind: FaultKind = field(default=FaultKind.TIMEOUT, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        exc = InjectedTimeout(
            self.dependency, self.name, f"timed out after {self.deadline_s:.3f}s"
        )
        return FaultEffect(
            delay_s=self.deadline_s, raises=exc, label=f"timeout@{self.deadline_s:.3f}s"
        )


@dataclass(frozen=True, slots=True)
class ConnectionDropFault(Fault):
    """Raise a connection-reset-style error immediately (no delay)."""

    message: str = "connection reset by peer"
    kind: FaultKind = field(default=FaultKind.CONNECTION_DROP, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        exc = InjectedConnectionError(self.dependency, self.name, self.message)
        return FaultEffect(raises=exc, label="connection_drop")


@dataclass(frozen=True, slots=True)
class PartialResponseFault(Fault):
    """Call through, then corrupt/truncate the *real* result via ``transform``.

    The default transform truncates ``str``/``bytes``/``list``/``dict`` results
    to ``keep_fraction`` of their length, modelling a short read / partial body.
    """

    keep_fraction: float = 0.5
    transform_fn: Callable[[Any], Any] | None = None
    kind: FaultKind = field(default=FaultKind.PARTIAL_RESPONSE, init=False)

    def _truncate(self, result: Any) -> Any:
        frac = max(0.0, min(1.0, self.keep_fraction))
        if isinstance(result, (str, bytes)):
            return result[: int(len(result) * frac)]
        if isinstance(result, list):
            return result[: int(len(result) * frac)]
        if isinstance(result, dict):
            keep = int(len(result) * frac)
            return dict(list(result.items())[:keep])
        return result

    def compute(self, ctx: FaultContext) -> FaultEffect:
        fn = self.transform_fn or self._truncate
        return FaultEffect(transform=fn, label=f"partial({self.keep_fraction:.2f})")


@dataclass(frozen=True, slots=True)
class ClockSkewFault(Fault):
    """Surface a skewed ``now`` to clock-reading callers (no raise, no delay).

    ``skew_s`` is added to the caller's perceived clock — models a dependency (or
    the host) whose clock has drifted, which breaks TTLs / token expiry / dedup
    windows. The interceptor exposes ``effect.skew_s`` to the call.
    """

    skew_s_value: float = 30.0
    kind: FaultKind = field(default=FaultKind.CLOCK_SKEW, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        return FaultEffect(skew_s=self.skew_s_value, label=f"clock_skew{self.skew_s_value:+.1f}s")


@dataclass(frozen=True, slots=True)
class DependencyDownFault(Fault):
    """Hard down: every call to the scoped dependency fails fast for the window."""

    message: str = "dependency unavailable"
    kind: FaultKind = field(default=FaultKind.DEPENDENCY_DOWN, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        exc = InjectedConnectionError(
            self.dependency, self.name, f"{self.dependency}: {self.message}"
        )
        return FaultEffect(raises=exc, label="dependency_down")


@dataclass(frozen=True, slots=True)
class RateLimitStormFault(Fault):
    """After ``allow_first`` calls, 429 every subsequent call in the window.

    Models a provider rate-limit storm (kinora.md notes the 429
    ``Throttling.RateQuota`` on the image model): the first few calls succeed,
    then the dependency starts throttling. Stateless across the fault object —
    state comes from ``ctx.call_index`` so it stays deterministic + frozen.
    """

    allow_first: int = 3
    retry_after_s: float = 2.0
    kind: FaultKind = field(default=FaultKind.RATE_LIMIT_STORM, init=False)

    def compute(self, ctx: FaultContext) -> FaultEffect:
        if ctx.call_index < self.allow_first:
            return FaultEffect(label=f"rate_ok({ctx.call_index})")
        exc = InjectedRateLimit(
            self.dependency,
            self.name,
            "429 Throttling.RateQuota (injected storm)",
            retry_after_s=self.retry_after_s,
        )
        return FaultEffect(raises=exc, label="rate_limit_429")


__all__ = [
    "ClockSkewFault",
    "ConnectionDropFault",
    "DependencyDownFault",
    "ErrorFault",
    "Fault",
    "FaultContext",
    "FaultEffect",
    "FaultKind",
    "InjectedConnectionError",
    "InjectedFault",
    "InjectedRateLimit",
    "InjectedTimeout",
    "LatencyFault",
    "PartialResponseFault",
    "RateLimitStormFault",
    "TimeoutFault",
]
