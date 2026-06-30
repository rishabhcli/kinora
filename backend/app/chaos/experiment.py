"""The chaos *experiment* (scenario) model — Chaos-Engineering principles as data.

A :class:`ChaosExperiment` is a declarative scenario the game-day runner
executes. It bundles the four Chaos-Engineering ingredients:

1. **Hypothesis** — the :class:`~app.chaos.steady_state.SteadyStateHypothesis`
   the system should keep satisfying while faults are active.
2. **Blast radius** — the set of dependency names chaos is allowed to touch; the
   injector refuses anything outside it (defence in depth).
3. **Fault schedule** — an ordered list of :class:`ScheduledFault` entries, each
   arming a fault at an offset and (optionally) holding it for a duration.
4. **Abort conditions** — when to halt early: always on a steady-state breach
   (the auto-abort), plus optional caps (max injected errors, max wall-time).

The model is pure data + validation; it performs no I/O. Validation catches the
classic foot-guns up front: a fault scoped to a dependency outside the blast
radius, a hold that ends before it starts, an empty schedule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.chaos.faults import Fault
from app.chaos.steady_state import SteadyStateHypothesis


@dataclass(frozen=True, slots=True)
class ScheduledFault:
    """One entry of the fault schedule: arm ``fault`` at ``arm_at_s``.

    ``hold_s`` is how long (virtual seconds, from ``arm_at_s``) the fault stays
    armed before the runner disarms it; ``None`` means "hold until the experiment
    ends". Offsets are relative to the start of the fault-injection phase (after
    the initial steady-state confirmation).
    """

    fault: Fault
    arm_at_s: float = 0.0
    hold_s: float | None = None

    @property
    def disarm_at_s(self) -> float | None:
        """Absolute offset at which to disarm, or ``None`` to hold to the end."""
        return None if self.hold_s is None else self.arm_at_s + self.hold_s

    def __post_init__(self) -> None:
        if self.arm_at_s < 0:
            raise ValueError("ScheduledFault.arm_at_s must be >= 0")
        if self.hold_s is not None and self.hold_s <= 0:
            raise ValueError("ScheduledFault.hold_s must be > 0 when set")


@dataclass(frozen=True, slots=True)
class AbortConditions:
    """When the runner halts a scenario early (besides the always-on auto-abort).

    The steady-state breach auto-abort is *not* configurable here — it always
    fires (it is the whole point). These are *additional* guardrails so a runaway
    experiment cannot burn unbounded time / inject unbounded failures.
    """

    #: Halt after this many injected exceptions across the run (``None`` = no cap).
    max_injected_errors: int | None = None
    #: Halt after this much virtual wall-time elapses (``None`` = no cap).
    max_duration_s: float | None = None
    #: Number of consecutive breaching polls tolerated before abort. ``1`` means
    #: the very first breach aborts; ``>1`` rides out transient blips.
    breach_tolerance: int = 1

    def __post_init__(self) -> None:
        if self.breach_tolerance < 1:
            raise ValueError("breach_tolerance must be >= 1")
        if self.max_injected_errors is not None and self.max_injected_errors < 0:
            raise ValueError("max_injected_errors must be >= 0 when set")
        if self.max_duration_s is not None and self.max_duration_s <= 0:
            raise ValueError("max_duration_s must be > 0 when set")


@dataclass(frozen=True, slots=True)
class ChaosExperiment:
    """A complete, declarative chaos scenario the runner executes.

    ``poll_interval_s`` is how often the runner samples the steady-state probe
    during the injection phase (also how finely the schedule is honoured).
    ``duration_s`` is the total injection-phase length. ``seed`` makes the whole
    run reproducible.
    """

    name: str
    hypothesis: SteadyStateHypothesis
    blast_radius: frozenset[str]
    schedule: tuple[ScheduledFault, ...]
    abort: AbortConditions = field(default_factory=AbortConditions)
    duration_s: float = 30.0
    poll_interval_s: float = 1.0
    seed: int = 1337
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("experiment needs a name")
        if not self.schedule:
            raise ValueError("experiment needs at least one scheduled fault")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if self.poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        if not self.blast_radius:
            raise ValueError("experiment needs a non-empty blast radius")
        # Every scheduled fault must be scoped within the declared blast radius.
        for entry in self.schedule:
            if entry.fault.dependency not in self.blast_radius:
                raise ValueError(
                    f"fault {entry.fault.name!r} targets dependency "
                    f"{entry.fault.dependency!r} outside the blast radius "
                    f"{sorted(self.blast_radius)}"
                )
            if entry.arm_at_s >= self.duration_s:
                raise ValueError(
                    f"fault {entry.fault.name!r} arms at {entry.arm_at_s}s, "
                    f"at/after the experiment duration {self.duration_s}s"
                )

    @property
    def faults(self) -> tuple[Fault, ...]:
        """All faults referenced by the schedule (arm order)."""
        return tuple(entry.fault for entry in self.schedule)

    @staticmethod
    def of(
        name: str,
        *,
        hypothesis: SteadyStateHypothesis,
        blast_radius: Sequence[str],
        schedule: Sequence[ScheduledFault],
        abort: AbortConditions | None = None,
        duration_s: float = 30.0,
        poll_interval_s: float = 1.0,
        seed: int = 1337,
        description: str = "",
    ) -> ChaosExperiment:
        """Convenience constructor accepting plain sequences for the collections."""
        return ChaosExperiment(
            name=name,
            hypothesis=hypothesis,
            blast_radius=frozenset(blast_radius),
            schedule=tuple(schedule),
            abort=abort or AbortConditions(),
            duration_s=duration_s,
            poll_interval_s=poll_interval_s,
            seed=seed,
            description=description,
        )


__all__ = ["AbortConditions", "ChaosExperiment", "ScheduledFault"]
