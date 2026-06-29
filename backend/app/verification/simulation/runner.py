"""The sweep + shrink + replay driver — FoundationDB's "run thousands of seeds,
minimise the one that broke, replay it forever" workflow (kinora.md §12, §13).

This is the top of the simulation framework: it turns the engine, the seams, the
system wiring, and the invariants into the verification loop a reviewer actually
runs.

* :func:`run_seed` — execute one ``(seed, profile)`` end-to-end and check the
  invariants. The atom of the sweep, and the thing :func:`replay` re-runs.
* :func:`sweep` — run *N* seeds across the reader archetypes under a profile,
  stopping at the first invariant violation (or exhausting the budget cleanly).
  This is the "thousands of seeded fault schedules" workhorse.
* :func:`shrink` — given a failing :class:`~app.verification.simulation.faults.FaultSchedule`,
  search for the *minimal* adversary that still reproduces the violation: turn the
  global intensity down, then drop fault kinds one at a time, keeping any change
  that preserves the failure. A minimal schedule is the difference between a bug
  report that says "1,000 faults fired, one of them mattered" and one that says
  "this bug needs exactly a REDIS_ERROR at 6% and nothing else."
* :func:`replay` — re-run a (possibly shrunken) schedule and return its full
  report, so a saved failing seed reproduces to the byte.

The shrinker is delta-debugging specialised to fault schedules. It exploits the
two levers a :class:`FaultProfile` exposes — global intensity and per-kind
enable/disable — which is exactly the search space FoundationDB shrinks over
(fewer Buggify hits, lower probabilities). Because the run is a pure function of
the schedule, every shrink step is a deterministic re-check: no flakiness, no
"it reproduced that time".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.verification.simulation.faults import (
    FaultProfile,
    FaultSchedule,
)
from app.verification.simulation.invariants import (
    CORE_INVARIANTS,
    QUALITY_INVARIANTS,
    Invariant,
    InvariantReport,
    check_invariants,
)
from app.verification.simulation.runtime import Simulation
from app.verification.simulation.system import SystemConfig, SystemReport, run_system
from app.verification.simulation.workload import ARCHETYPES

logger = get_logger("app.verification.simulation.runner")


@dataclass(slots=True)
class SeedResult:
    """The outcome of running one seed: the schedule, its invariant report, the
    fault histogram, and a back-reference to the system report for inspection."""

    schedule: FaultSchedule
    archetype: str
    invariants: InvariantReport
    fault_summary: str
    system: SystemReport

    @property
    def ok(self) -> bool:
        return self.invariants.ok

    def describe(self) -> str:
        return (
            f"seed={self.schedule.seed} archetype={self.archetype} "
            f"profile={self.schedule.profile.label} faults=[{self.fault_summary}] "
            f"=> {self.invariants.summary()}"
        )


def run_seed(
    seed: int,
    profile: FaultProfile,
    *,
    archetype: str = "steady",
    config: SystemConfig | None = None,
    invariants: tuple[Invariant, ...] = CORE_INVARIANTS,
    include_quality: bool = False,
) -> SeedResult:
    """Execute one ``(seed, profile, archetype)`` run and check its invariants.

    ``include_quality`` adds the profile-gated buffer-health invariant (only
    meaningful under light fault load). The system config's ``archetype`` is
    overridden by the ``archetype`` argument so a sweep can fan the same seed
    across reader types.
    """
    cfg = config or SystemConfig()
    # Honour the requested archetype without mutating the caller's config.
    if cfg.archetype != archetype:
        cfg = _with_archetype(cfg, archetype)

    schedule = FaultSchedule(seed=seed, profile=profile)
    with Simulation(schedule) as sim:
        report = run_system(sim, cfg)
        suite = invariants + (QUALITY_INVARIANTS if include_quality else ())
        inv = check_invariants(report, invariants=suite)
        return SeedResult(
            schedule=schedule,
            archetype=archetype,
            invariants=inv,
            fault_summary=sim.buggify_log.summary(),
            system=report,
        )


@dataclass(slots=True)
class SweepResult:
    """The outcome of a seed sweep: every result, and the first failure (if any)."""

    profile_label: str
    ran: int = 0
    passed: int = 0
    results: list[SeedResult] = field(default_factory=list)
    first_failure: SeedResult | None = None

    @property
    def ok(self) -> bool:
        return self.first_failure is None

    def summary(self) -> str:
        if self.ok:
            return (
                f"sweep[{self.profile_label}]: {self.passed}/{self.ran} seeds passed "
                "(all invariants held)"
            )
        f = self.first_failure
        assert f is not None
        return (
            f"sweep[{self.profile_label}]: FAILED after {self.ran} seeds — "
            f"{f.describe()}"
        )


def sweep(
    *,
    profile: FaultProfile,
    seeds: Iterable[int],
    archetypes: tuple[str, ...] = ARCHETYPES,
    config: SystemConfig | None = None,
    invariants: tuple[Invariant, ...] = CORE_INVARIANTS,
    stop_on_first_failure: bool = True,
    include_quality: bool = False,
) -> SweepResult:
    """Run many seeds × archetypes under ``profile``, checking invariants.

    The workhorse of the verification loop: the "thousands of seeded fault
    schedules" of the brief. By default it stops at the first violation (so a
    regression surfaces a minimal-to-find failing seed fast), but
    ``stop_on_first_failure=False`` runs the whole grid to *count* violations
    (useful for characterising a known-leaky invariant without aborting).
    """
    result = SweepResult(profile_label=profile.label)
    for seed in seeds:
        for archetype in archetypes:
            seed_res = run_seed(
                seed,
                profile,
                archetype=archetype,
                config=config,
                invariants=invariants,
                include_quality=include_quality,
            )
            result.ran += 1
            result.results.append(seed_res)
            if seed_res.ok:
                result.passed += 1
            else:
                if result.first_failure is None:
                    result.first_failure = seed_res
                    logger.info(
                        "sweep.violation",
                        seed=seed,
                        archetype=archetype,
                        violation=seed_res.invariants.summary(),
                    )
                if stop_on_first_failure:
                    return result
    return result


# --------------------------------------------------------------------------- #
# Shrinking — minimise a failing schedule to the smallest reproducing adversary
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ShrinkResult:
    """A shrunken failing schedule plus how far it was minimised."""

    original: FaultSchedule
    minimal: FaultSchedule
    violation: str
    archetype: str
    steps: int = 0

    def describe(self) -> str:
        return (
            f"shrunk {self.original.profile.label} "
            f"({len(self.original.profile.active_kinds())} kinds, "
            f"intensity {self.original.profile.intensity:g}) → "
            f"({len(self.minimal.profile.active_kinds())} kinds, "
            f"intensity {self.minimal.profile.intensity:g}) in {self.steps} steps; "
            f"reproduces: {self.violation}\n  {self.minimal.describe()}"
        )


def _reproduces(
    schedule: FaultSchedule,
    archetype: str,
    target_violation: str,
    *,
    config: SystemConfig | None,
    invariants: tuple[Invariant, ...],
) -> bool:
    """Whether ``schedule`` still fails the *same* invariant as the original.

    The shrinker must preserve the *specific* failure, not merely *a* failure —
    otherwise it could drift to a different bug and report a misleading minimal
    schedule. We key on the violated invariant's name.
    """
    res = run_seed(
        schedule.seed,
        schedule.profile,
        archetype=archetype,
        config=config,
        invariants=invariants,
    )
    v = res.invariants.first_violation()
    return v is not None and v.name == target_violation


def shrink(
    failing: SeedResult,
    *,
    config: SystemConfig | None = None,
    invariants: tuple[Invariant, ...] = CORE_INVARIANTS,
    max_steps: int = 200,
) -> ShrinkResult:
    """Delta-debug a failing run to the minimal adversary that still reproduces it.

    Two reductions, applied greedily and repeatedly until a fixpoint:

    #. **Drop fault kinds.** For each active kind, try disabling it; keep the
       schedule disabled if the *same* violation still reproduces. This isolates
       which faults are actually necessary (typically one or two of a storm of a
       dozen).
    #. **Lower global intensity.** Binary-search the intensity multiplier down
       toward the smallest value that still reproduces — proving whether the bug
       needs a heavy storm or just a whisper of one fault.

    The result is a schedule a reviewer can paste into :func:`replay` and re-hit
    forever. The seed is *never* changed (the failure is seed-anchored); only the
    profile is minimised.
    """
    violation = failing.invariants.first_violation()
    assert violation is not None, "shrink() requires a failing SeedResult"
    target = violation.name
    archetype = failing.archetype
    seed = failing.schedule.seed

    current = failing.schedule.profile
    steps = 0

    # --- reduction 1: drop unnecessary fault kinds ------------------------- #
    changed = True
    while changed and steps < max_steps:
        changed = False
        for kind in list(current.active_kinds()):
            candidate = current.disabling(kind)
            steps += 1
            if _reproduces(
                FaultSchedule(seed=seed, profile=candidate),
                archetype,
                target,
                config=config,
                invariants=invariants,
            ):
                current = candidate
                changed = True
            if steps >= max_steps:
                break

    # --- reduction 2: lower global intensity ------------------------------- #
    lo, hi = 0.0, current.intensity
    # Binary-search the smallest intensity in (lo, hi] that still reproduces.
    for _ in range(24):
        if steps >= max_steps:
            break
        mid = (lo + hi) / 2.0
        if mid <= 0.0:
            break
        steps += 1
        if _reproduces(
            FaultSchedule(seed=seed, profile=current.with_intensity(mid)),
            archetype,
            target,
            config=config,
            invariants=invariants,
        ):
            hi = mid  # still reproduces at a lower intensity → push lower
        else:
            lo = mid  # too gentle → back off
    current = current.with_intensity(hi)

    minimal = FaultSchedule(seed=seed, profile=current)
    return ShrinkResult(
        original=failing.schedule,
        minimal=minimal,
        violation=violation.summary() if hasattr(violation, "summary") else violation.detail,
        archetype=archetype,
        steps=steps,
    )


# --------------------------------------------------------------------------- #
# Replay — reproduce a saved schedule to the byte
# --------------------------------------------------------------------------- #


def replay(
    schedule: FaultSchedule,
    *,
    archetype: str = "steady",
    config: SystemConfig | None = None,
    invariants: tuple[Invariant, ...] = CORE_INVARIANTS,
) -> SeedResult:
    """Re-run a (possibly shrunken) schedule and return its full result.

    Because the run is a pure function of ``(seed, profile)`` and all entropy is
    owned (see :mod:`~app.verification.simulation.determinism`), this reproduces
    the original run exactly — the foundation of a durable regression test: save
    the minimal schedule, replay it in CI forever.
    """
    return run_seed(
        schedule.seed,
        schedule.profile,
        archetype=archetype,
        config=config,
        invariants=invariants,
    )


def _with_archetype(config: SystemConfig, archetype: str) -> SystemConfig:
    """Return a copy of ``config`` with a different reader archetype."""
    from dataclasses import replace

    return replace(config, archetype=archetype)


__all__ = [
    "SeedResult",
    "ShrinkResult",
    "SweepResult",
    "replay",
    "run_seed",
    "shrink",
    "sweep",
]
