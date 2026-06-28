"""Named run profiles — scenario + workload + SLOs in one preset (kinora.md §4/§12).

A *profile* is the unit the CLI's ``--profile`` flag resolves: it bundles a
:class:`~app.reliability.scenarios.Scenario`, a
:class:`~app.reliability.workload.WorkloadPlan` template (parameterised by the
CLI's ``--users`` / ``--duration`` / ``--rps`` overrides), and the
:class:`~app.reliability.slo.SLOSet` to gate the result against. This keeps the
CLI dumb and the presets unit-testable.

The presets cover the four shapes a reliability run wants against
generation-on-scroll: a steady closed-model soak, a fast-skim storm, a seek
thrash, and an open-model warm-up→spike that actually exercises backpressure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.reliability.scenarios import Scenario, get_scenario
from app.reliability.slo import SLOSet, default_kinora_slos
from app.reliability.workload import (
    RampProfile,
    RampShape,
    ThinkTime,
    WorkloadKind,
    WorkloadPlan,
)


@dataclass(frozen=True, slots=True)
class RunProfile:
    """A named load-run preset (scenario + workload template + SLOs)."""

    name: str
    description: str
    scenario_name: str
    workload_kind: WorkloadKind
    #: Builds the workload plan from the CLI overrides.
    build_workload: Callable[[ProfileOverrides], WorkloadPlan]
    slos: SLOSet

    def scenario(self) -> Scenario:
        """Resolve the bound scenario."""
        return get_scenario(self.scenario_name)


@dataclass(frozen=True, slots=True)
class ProfileOverrides:
    """The CLI knobs that parameterise a profile's workload template."""

    users: int = 16
    duration_s: float = 60.0
    rate_rps: float = 0.0  # 0 => use the profile's closed/open default
    seed: int = 1337


def _steady_soak() -> RunProfile:
    def build(o: ProfileOverrides) -> WorkloadPlan:
        return WorkloadPlan.closed(
            users=o.users,
            duration_s=o.duration_s,
            think=ThinkTime(mean_s=1.0, jitter_s=0.3),
            ramp=RampProfile(shape=RampShape.LINEAR, ramp_s=5.0, floor=0.2),
        )

    return RunProfile(
        name="steady_soak",
        description="Closed-model soak of engaged readers (the §4.5 happy-path sawtooth).",
        scenario_name="steady_reader",
        workload_kind=WorkloadKind.CLOSED,
        build_workload=build,
        slos=default_kinora_slos(),
    )


def _skim_storm() -> RunProfile:
    def build(o: ProfileOverrides) -> WorkloadPlan:
        return WorkloadPlan.closed(
            users=o.users,
            duration_s=o.duration_s,
            think=ThinkTime(mean_s=0.5, jitter_s=0.2),
            ramp=RampProfile(shape=RampShape.STEP, ramp_s=3.0, floor=0.0),
        )

    return RunProfile(
        name="skim_storm",
        description="Heavy skimmers; promotion suspended, keyframe ladder under load.",
        scenario_name="skim_storm",
        workload_kind=WorkloadKind.CLOSED,
        build_workload=build,
        slos=default_kinora_slos(),
    )


def _seek_thrash() -> RunProfile:
    def build(o: ProfileOverrides) -> WorkloadPlan:
        return WorkloadPlan.closed(
            users=o.users,
            duration_s=o.duration_s,
            think=ThinkTime(mean_s=0.8, jitter_s=0.3),
        )

    return RunProfile(
        name="seek_thrash",
        description="Constant far seeks; stresses cancellation + the instant bridge (§4.8).",
        scenario_name="seek_thrash",
        workload_kind=WorkloadKind.CLOSED,
        build_workload=build,
        slos=default_kinora_slos(),
    )


def _open_spike() -> RunProfile:
    def build(o: ProfileOverrides) -> WorkloadPlan:
        rate = o.rate_rps if o.rate_rps > 0.0 else float(o.users)
        return WorkloadPlan.open(
            rate_rps=rate,
            duration_s=o.duration_s,
            ramp=RampProfile(
                shape=RampShape.SPIKE,
                spike_mult=3.0,
                spike_start_s=o.duration_s * 0.5,
                spike_len_s=o.duration_s * 0.2,
            ),
            seed=o.seed,
        )

    return RunProfile(
        name="open_spike",
        description="Open-model warm-up then a 3x spike; exercises backpressure (§12.2).",
        scenario_name="steady_reader",
        workload_kind=WorkloadKind.OPEN,
        build_workload=build,
        slos=default_kinora_slos(),
    )


def _cold_open() -> RunProfile:
    def build(o: ProfileOverrides) -> WorkloadPlan:
        # All readers arrive at once (step ramp, no warm-up): the §4.10 t=0 burst.
        return WorkloadPlan.closed(
            users=o.users,
            duration_s=o.duration_s,
            think=ThinkTime(mean_s=1.0, jitter_s=0.1),
            ramp=RampProfile(shape=RampShape.STEP, ramp_s=0.0, floor=1.0),
        )

    return RunProfile(
        name="cold_open",
        description="Synchronized cold start; the initial committed burst to H (§4.10).",
        scenario_name="cold_open",
        workload_kind=WorkloadKind.CLOSED,
        build_workload=build,
        slos=default_kinora_slos(),
    )


#: The named-profile registry the CLI ``--profile`` resolves against.
def profile_registry() -> dict[str, RunProfile]:
    """Build the registry of named run profiles."""
    profiles = [
        _steady_soak(),
        _skim_storm(),
        _seek_thrash(),
        _open_spike(),
        _cold_open(),
    ]
    return {p.name: p for p in profiles}


def get_profile(name: str) -> RunProfile:
    """Resolve a run profile by name (``ValueError`` for an unknown name)."""
    registry = profile_registry()
    profile = registry.get(name)
    if profile is None:
        raise ValueError(
            f"unknown profile {name!r}; known: {', '.join(sorted(registry))}"
        )
    return profile


__all__ = [
    "ProfileOverrides",
    "RunProfile",
    "get_profile",
    "profile_registry",
]
