"""Smoke-test gating before a rollout advances (kinora.md §12.6 / §17).

After a new version is staged (green up at 0%, or the first canary slice) the
orchestrator runs a **smoke suite** against it before shifting real traffic. The
Kinora-shaped smoke suite proves the deploy is alive end-to-end *without
spending credits*:

* ``GET /health`` returns ok (liveness),
* ``GET /ready`` returns ready with postgres + redis checks true (§main.py),
* the provider preflight passes (``make provider-preflight`` — the safe hosted
  diagnostics, **no** ``--spend-smoke``), and
* one shot can be *enqueued and degraded to Ken-Burns* (the off-gate path), so
  the render-worker drains the queue without touching Wan.

Each check is a :class:`SmokeCheck` — a name + an async predicate returning a
:class:`SmokeOutcome`. The gate runs them, optionally short-circuiting on the
first failure, and reports a :class:`SmokeReport`. Production wires checks that
make real (cheap, non-spending) calls; tests inject scripted checks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SmokeOutcome:
    passed: bool
    detail: str = ""
    latency_ms: float = 0.0

    @classmethod
    def ok(cls, detail: str = "", latency_ms: float = 0.0) -> SmokeOutcome:
        return cls(passed=True, detail=detail, latency_ms=latency_ms)

    @classmethod
    def fail(cls, detail: str, latency_ms: float = 0.0) -> SmokeOutcome:
        return cls(passed=False, detail=detail, latency_ms=latency_ms)


@dataclass(frozen=True, slots=True)
class SmokeCheck:
    """A single named smoke check.

    ``required`` checks fail the whole gate when they fail; non-required checks
    are advisory (their failure is reported but does not block the rollout).
    """

    name: str
    run: Callable[[str], Awaitable[SmokeOutcome]]
    required: bool = True


@dataclass(frozen=True, slots=True)
class CheckRun:
    name: str
    required: bool
    outcome: SmokeOutcome


@dataclass(frozen=True, slots=True)
class SmokeReport:
    target: str
    runs: tuple[CheckRun, ...]

    @property
    def passed(self) -> bool:
        """The gate passes iff every *required* check passed."""
        return all(r.outcome.passed for r in self.runs if r.required)

    @property
    def failures(self) -> list[CheckRun]:
        return [r for r in self.runs if not r.outcome.passed]

    @property
    def blocking_failures(self) -> list[CheckRun]:
        return [r for r in self.runs if r.required and not r.outcome.passed]

    def summary(self) -> str:
        ran = len(self.runs)
        bad = len(self.failures)
        return f"smoke {self.target}: {ran - bad}/{ran} passed"


@dataclass(slots=True)
class SmokeGate:
    """Runs an ordered :class:`SmokeCheck` suite against a target."""

    checks: Sequence[SmokeCheck]
    short_circuit: bool = True

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError("SmokeGate requires at least one check")
        names = [c.name for c in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("smoke check names must be unique")

    async def run(self, target: str) -> SmokeReport:
        runs: list[CheckRun] = []
        for check in self.checks:
            try:
                outcome = await check.run(target)
            except Exception as exc:  # a throwing check is a failure, not a crash
                outcome = SmokeOutcome.fail(f"{type(exc).__name__}: {exc}")
            runs.append(CheckRun(name=check.name, required=check.required, outcome=outcome))
            if self.short_circuit and check.required and not outcome.passed:
                break
        return SmokeReport(target=target, runs=tuple(runs))


# A small library of constructors for the canonical Kinora smoke checks. The
# actual probing callables are injected (so no network here); these just bind a
# name + required flag to a caller-supplied async predicate.


def health_check(run: Callable[[str], Awaitable[SmokeOutcome]]) -> SmokeCheck:
    return SmokeCheck(name="health", run=run, required=True)


def readiness_check(run: Callable[[str], Awaitable[SmokeOutcome]]) -> SmokeCheck:
    return SmokeCheck(name="readiness", run=run, required=True)


def provider_preflight_check(run: Callable[[str], Awaitable[SmokeOutcome]]) -> SmokeCheck:
    # Non-spending hosted diagnostics; advisory in dev, required in prod by
    # passing required=True at the call site if desired.
    return SmokeCheck(name="provider-preflight", run=run, required=True)


def degraded_render_check(run: Callable[[str], Awaitable[SmokeOutcome]]) -> SmokeCheck:
    # Enqueue one shot and prove it degrades to Ken-Burns with the gate off
    # (no Wan spend). Required: it exercises the queue → worker → OSS path.
    return SmokeCheck(name="degraded-render", run=run, required=True)


@dataclass(slots=True)
class ScriptedSmokeCheck:
    """A test/simulator helper: a check that returns a fixed outcome.

    Usable directly as the ``run`` callable for a :class:`SmokeCheck`.
    """

    outcome: SmokeOutcome
    calls: list[str] = field(default_factory=list)

    async def __call__(self, target: str) -> SmokeOutcome:
        self.calls.append(target)
        return self.outcome
