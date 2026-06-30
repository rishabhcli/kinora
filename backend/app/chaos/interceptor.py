"""The injectable fault interceptor — where armed faults meet real calls.

The :class:`FaultInjector` is the deterministic, in-process engine the whole
framework hangs off. It holds:

* a seeded RNG and an injected :class:`~app.chaos.clock.Clock` (determinism),
* a **blast-radius scope** — the set of dependency names chaos is *allowed* to
  touch; calls to any other dependency pass through untouched, and
* a registry of currently-**armed** faults, keyed by dependency.

Callers route their dependency calls through :meth:`FaultInjector.call` (or wrap
an async callable once with :meth:`FaultInjector.wrap`). The injector finds the
armed faults for that dependency, asks each for a :class:`FaultEffect`, performs
the effect (sleep via the clock → raise → call through → transform), records a
:class:`CallRecord` on the timeline, and returns the (possibly mutated) result.

Determinism is the point: given a seed + virtual clock, the exact sequence of
injected delays / errors / mutations replays identically, so a game-day asserts
a *specific* failure pattern rather than flaking. No real sleeping happens — the
virtual clock's ``sleep`` advances instantly.

Blast-radius scoping is enforced here (defence in depth): even if a fault is
armed for a dependency outside the active scope, :meth:`call` refuses to apply
it. This is what lets a scenario declare "only ``dashscope`` may break" and have
the framework guarantee Redis/Postgres calls stay clean.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.chaos.clock import SYSTEM_CLOCK, Clock
from app.chaos.faults import Fault, FaultContext, FaultEffect

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One row of the injector's call timeline (for the findings report)."""

    dependency: str
    call_index: int
    fault_name: str | None
    effect_label: str
    delay_s: float
    raised: str | None
    monotonic_at: float


@dataclass(slots=True)
class _ArmedFault:
    """An armed fault plus its arming time (for windowed faults' age math)."""

    fault: Fault
    armed_at: float


class FaultInjector:
    """The in-process, deterministic fault-injection engine.

    Construct with a seed and a :class:`Clock`; arm faults; route calls through
    :meth:`call`. Thread-safety is *not* claimed — a game-day drives it from one
    asyncio task at a time (the runner), which keeps the call-index sequence
    deterministic. The injector is reusable: :meth:`disarm_all` clears state.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        clock: Clock | None = None,
        scope: set[str] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._clock: Clock = clock or SYSTEM_CLOCK
        #: Dependencies chaos may touch. ``None`` == unrestricted (no scope set).
        self._scope: set[str] | None = set(scope) if scope is not None else None
        self._armed: dict[str, list[_ArmedFault]] = {}
        self._call_counts: dict[str, int] = {}
        self._timeline: list[CallRecord] = []

    # -- scope (blast radius) ------------------------------------------------

    def set_scope(self, dependencies: set[str] | None) -> None:
        """Restrict chaos to ``dependencies`` (``None`` lifts the restriction)."""
        self._scope = set(dependencies) if dependencies is not None else None

    def in_scope(self, dependency: str) -> bool:
        """Whether ``dependency`` is within the active blast radius."""
        return self._scope is None or dependency in self._scope

    @property
    def scope(self) -> set[str] | None:
        return None if self._scope is None else set(self._scope)

    # -- arming --------------------------------------------------------------

    def arm(self, fault: Fault) -> None:
        """Arm ``fault`` against its dependency from ``now`` until disarmed."""
        self._armed.setdefault(fault.dependency, []).append(
            _ArmedFault(fault=fault, armed_at=self._clock.monotonic())
        )

    def arm_many(self, faults: list[Fault]) -> None:
        for f in faults:
            self.arm(f)

    def disarm(self, dependency: str) -> None:
        """Remove all armed faults for one dependency."""
        self._armed.pop(dependency, None)

    def disarm_all(self) -> None:
        """Remove every armed fault (the runner's rollback primitive)."""
        self._armed.clear()

    @property
    def armed_dependencies(self) -> set[str]:
        return {dep for dep, faults in self._armed.items() if faults}

    # -- timeline ------------------------------------------------------------

    @property
    def timeline(self) -> list[CallRecord]:
        return list(self._timeline)

    def reset_timeline(self) -> None:
        self._timeline.clear()
        self._call_counts.clear()

    # -- the call seam -------------------------------------------------------

    def _next_effect(self, dependency: str) -> tuple[FaultEffect, str | None]:
        """Compute the combined effect of all armed faults for ``dependency``.

        Faults compose in arm order: a raise from an earlier fault short-circuits
        (the call never reaches a later fault's transform); delays accumulate.
        """
        index = self._call_counts.get(dependency, 0)
        self._call_counts[dependency] = index + 1

        if not self.in_scope(dependency):
            return FaultEffect(), None

        armed = self._armed.get(dependency, [])
        if not armed:
            return FaultEffect(), None

        total_delay = 0.0
        skew = 0.0
        transform: Callable[[Any], Any] | None = None
        raises: BaseException | None = None
        labels: list[str] = []
        fired_name: str | None = None
        now = self._clock.monotonic()

        for entry in armed:
            ctx = FaultContext(
                dependency=dependency,
                call_index=index,
                rng=self._rng,
                now=now,
                armed_at=entry.armed_at,
            )
            effect = entry.fault.apply(ctx)
            if effect.is_passthrough:
                continue
            fired_name = fired_name or entry.fault.name
            labels.append(effect.label)
            total_delay += effect.delay_s
            skew += effect.skew_s
            if effect.transform is not None:
                transform = effect.transform
            if effect.raises is not None:
                raises = effect.raises
                break  # a raise short-circuits later faults' effects

        combined = FaultEffect(
            delay_s=total_delay,
            raises=raises,
            transform=transform if raises is None else None,
            skew_s=skew,
            label="+".join(labels) if labels else "passthrough",
        )
        return combined, fired_name

    async def call(
        self,
        dependency: str,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Invoke ``fn(*args, **kwargs)`` through the chaos layer for ``dependency``.

        Applies the composed effect of any armed, in-scope faults: virtual-sleeps
        for the delay, raises an injected error instead of calling through, or
        calls through and (optionally) transforms the real result. Every call —
        faulted or clean — appends a :class:`CallRecord` to the timeline.
        """
        effect, fired = self._next_effect(dependency)
        index = self._call_counts[dependency] - 1

        if effect.delay_s > 0:
            await self._clock.sleep(effect.delay_s)

        raised_repr: str | None = None
        try:
            if effect.raises is not None:
                raised_repr = type(effect.raises).__name__
                raise effect.raises
            result = await fn(*args, **kwargs)
            if effect.transform is not None:
                result = effect.transform(result)
            return result
        finally:
            self._timeline.append(
                CallRecord(
                    dependency=dependency,
                    call_index=index,
                    fault_name=fired,
                    effect_label=effect.label,
                    delay_s=effect.delay_s,
                    raised=raised_repr,
                    monotonic_at=self._clock.monotonic(),
                )
            )

    def wrap(
        self, dependency: str, fn: Callable[..., Awaitable[T]]
    ) -> Callable[..., Awaitable[T]]:
        """Return a drop-in async wrapper that routes ``fn`` through :meth:`call`."""

        async def _wrapped(*args: Any, **kwargs: Any) -> T:
            return await self.call(dependency, fn, *args, **kwargs)

        return _wrapped

    def clock_skew_for(self, dependency: str) -> float:
        """Peek the current composed clock skew a clock-skew fault would apply.

        Lets a caller that *reads* a clock (rather than awaits a call) observe the
        injected skew without going through :meth:`call`. Does not advance the
        call index or record a timeline row.
        """
        if not self.in_scope(dependency):
            return 0.0
        skew = 0.0
        now = self._clock.monotonic()
        for entry in self._armed.get(dependency, []):
            ctx = FaultContext(
                dependency=dependency,
                call_index=self._call_counts.get(dependency, 0),
                rng=random.Random(0),  # peek only: never perturbs the live RNG
                now=now,
                armed_at=entry.armed_at,
            )
            # Force-trigger only clock-skew kinds for the peek (probability aside).
            effect = entry.fault.compute(ctx)
            skew += effect.skew_s
        return skew


__all__ = ["CallRecord", "FaultInjector"]
