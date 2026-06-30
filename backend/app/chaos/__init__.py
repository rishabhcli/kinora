"""Orchestrated chaos engineering & game-day framework (kinora.md §4.11, §12.1).

Kinora rides on Postgres, Redis, object storage, and several flaky external
model providers; §4.11 enumerates the failures it must absorb (a render fails →
DLQ → Ken-Burns, the provider rate-limits, a seek strands in-flight work) and
§12.1 builds the queue to survive them. To *prove* that resilience you run
**game-days**: arm a real fault against one dependency for a window, assert the
system's steady state holds, and roll the fault back the instant it doesn't.

This package is the **orchestrated, scenario-level** chaos layer (distinct from a
per-call random chaos injector): faults are named, scoped, arm/disarm-able
disruptions an *experiment* schedules and a *runner* drives while watching a
steady-state guard, with an auto-abort + rollback the moment the guard breaches.

Modules:

* :mod:`app.chaos.clock` — the virtual-clock seam (deterministic, no real sleep).
* :mod:`app.chaos.faults` — the fault library: latency, error, timeout,
  connection-drop, partial-response, clock-skew, dependency-down, rate-limit-storm.
* :mod:`app.chaos.interceptor` — the injectable, seeded, blast-radius-scoped
  in-process fault injector every dependency call routes through.
* :mod:`app.chaos.steady_state` — the steady-state hypothesis + SLO bounds guard.
* :mod:`app.chaos.experiment` — the declarative scenario model (hypothesis,
  blast radius, fault schedule, abort conditions).
* :mod:`app.chaos.gate` — the production hard gate (refuses to arm outside
  ``local``/``test``, even if a flag is set).
* :mod:`app.chaos.runner` — the game-day runner (gate → preflight → schedule →
  auto-abort + rollback → findings report).
* :mod:`app.chaos.report` — the findings report.
* :mod:`app.chaos.scenarios` — a catalogue of named Kinora game-days.

Everything is pure given its collaborators (a seeded RNG, an injected clock, a
caller-supplied steady-state probe), so the whole framework unit-tests with **no
infra, no network, and zero model spend**, and is hard-gated OFF outside a
local/test environment.
"""

from __future__ import annotations

from app.chaos.clock import SYSTEM_CLOCK, Clock, SystemClock, VirtualClock
from app.chaos.experiment import AbortConditions, ChaosExperiment, ScheduledFault
from app.chaos.faults import (
    ClockSkewFault,
    ConnectionDropFault,
    DependencyDownFault,
    ErrorFault,
    Fault,
    FaultContext,
    FaultEffect,
    FaultKind,
    InjectedConnectionError,
    InjectedFault,
    InjectedRateLimit,
    InjectedTimeout,
    LatencyFault,
    PartialResponseFault,
    RateLimitStormFault,
    TimeoutFault,
)
from app.chaos.gate import (
    CHAOS_SAFE_ENVIRONMENTS,
    ChaosDisarmedError,
    GateDecision,
    assert_chaos_armable,
    evaluate_gate,
)
from app.chaos.interceptor import CallRecord, FaultInjector
from app.chaos.report import FindingsReport, SteadyStateSample, Verdict
from app.chaos.runner import GameDayRunner, SteadyStateProbe
from app.chaos.steady_state import (
    BoundResult,
    Comparison,
    SteadyStateBound,
    SteadyStateHypothesis,
    SteadyStateResult,
    availability_at_least,
    error_rate_at_most,
    latency_at_most,
)

__all__ = [
    "CHAOS_SAFE_ENVIRONMENTS",
    "SYSTEM_CLOCK",
    "AbortConditions",
    "BoundResult",
    "CallRecord",
    "ChaosDisarmedError",
    "ChaosExperiment",
    "ClockSkewFault",
    "Clock",
    "Comparison",
    "ConnectionDropFault",
    "DependencyDownFault",
    "ErrorFault",
    "Fault",
    "FaultContext",
    "FaultEffect",
    "FaultInjector",
    "FaultKind",
    "FindingsReport",
    "GameDayRunner",
    "GateDecision",
    "InjectedConnectionError",
    "InjectedFault",
    "InjectedRateLimit",
    "InjectedTimeout",
    "LatencyFault",
    "PartialResponseFault",
    "RateLimitStormFault",
    "ScheduledFault",
    "SteadyStateBound",
    "SteadyStateHypothesis",
    "SteadyStateProbe",
    "SteadyStateResult",
    "SteadyStateSample",
    "SystemClock",
    "TimeoutFault",
    "Verdict",
    "VirtualClock",
    "assert_chaos_armable",
    "availability_at_least",
    "error_rate_at_most",
    "evaluate_gate",
    "latency_at_most",
]
