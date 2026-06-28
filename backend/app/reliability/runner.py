"""The async load engine — drives scenarios against a transport (kinora.md §4/§12).

This is the reusable core the ``loadtest`` CLI wraps. It takes a
:class:`~app.reliability.scenarios.Scenario`, a
:class:`~app.reliability.workload.WorkloadPlan`, and a
:class:`~app.reliability.transport.Transport`, spins virtual readers per the
workload model, issues their planned requests, and folds every outcome into a
:class:`~app.reliability.metrics_report.LoadReport`.

It is **clock- and sleep-injected** so it is unit-testable without real time or
real network: the tests pass a :class:`FakeTransport`, a virtual clock, and an
instant sleep, then assert the report and the recorded calls. Production passes
``time.monotonic`` + ``asyncio.sleep`` + an :class:`HttpxTransport`.

Two models (§12.2):

* **Closed** — ``users`` looping virtual readers, each running a bound scenario
  session, paced by the model's think-time. Concurrency is capped at ``users``.
* **Open** — a Poisson arrival schedule fires *new* one-shot requests at a target
  rate; offered load is independent of server speed (the model that exposes
  backpressure). Each arrival issues one intent against a shared session pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.reliability.metrics_report import LoadReport, RequestOutcome
from app.reliability.reader_model import SETTLE_INTERVAL_S, ActionKind
from app.reliability.scenarios import (
    PlannedRequest,
    Scenario,
    ScenarioSession,
)
from app.reliability.transport import Response, Transport
from app.reliability.workload import WorkloadKind, WorkloadPlan

#: An async sleep seam: ``await sleep(seconds)``. Production: ``asyncio.sleep``.
SleepFn = Callable[[float], Awaitable[None]]
#: A monotonic clock seam returning seconds. Production: ``time.monotonic``.
ClockFn = Callable[[], float]


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Static configuration for one load run."""

    book_id: str = "book_demo"
    seed: int = 1337
    token: str | None = None
    #: Optional Bearer header injected on every request.
    headers: dict[str, str] = field(default_factory=dict)

    def request_headers(self) -> dict[str, str]:
        """The headers to attach to each request (token + extras)."""
        h = dict(self.headers)
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h


class LoadRunner:
    """Drives a scenario under a workload model against a transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        clock: ClockFn,
        sleep: SleepFn,
        config: RunnerConfig | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._config = config or RunnerConfig()

    # -- a single planned request ------------------------------------------- #

    async def _issue(self, report: LoadReport, planned: PlannedRequest) -> Response:
        """Issue one planned request and fold its outcome into the report."""
        resp = await self._transport.request(
            planned.method,
            planned.path,
            json=planned.json,
            headers=self._config.request_headers() or None,
        )
        report.record(
            RequestOutcome(
                endpoint=planned.endpoint,
                status=resp.status,
                latency_ms=resp.elapsed_ms,
                ok=planned.is_ok(resp),
                error=resp.error,
            )
        )
        return resp

    # -- closed model -------------------------------------------------------- #

    async def _run_closed_user(
        self,
        scenario: Scenario,
        report: LoadReport,
        *,
        user_index: int,
        duration_s: float,
    ) -> None:
        """One looping virtual reader: open a session, then stream its requests."""
        session = scenario.session(
            session_id=f"sess_load_{user_index:05d}",
            book_id=self._config.book_id,
            seed=self._config.seed + user_index,
        )
        # Prologue: open the session (counts toward the report).
        await self._issue(report, session.prologue())
        # Stream the reader's requests, paced by the model clock vs the run clock.
        start = self._clock()
        for planned, gap_s in self._paced(session, duration_s=duration_s):
            now = self._clock()
            elapsed = now - start
            if elapsed >= duration_s:
                break
            if gap_s > 0.0:
                await self._sleep(gap_s)
            await self._issue(report, planned)

    @staticmethod
    def _paced(
        session: ScenarioSession, *, duration_s: float
    ) -> list[tuple[PlannedRequest, float]]:
        """Materialize a reader's stream with inter-request think-gaps.

        The reader model already advances its own clock by a settle interval per
        step; here we recover the gap between successive *issued* requests (idle
        steps widen the gap), so the runner sleeps the right amount between calls.
        """
        out: list[tuple[PlannedRequest, float]] = []
        # Re-drive the model to recover timestamps alongside planned requests.
        model = session._model  # noqa: SLF001 - same package collaborator
        last_t = 0.0
        for action in model.steps(duration_s=duration_s):
            if action.kind is ActionKind.IDLE:
                continue  # no request; the gap to the next issued request grows
            planned = session.action_to_request(action)
            if planned is None:
                continue
            gap = max(0.0, action.t_s - last_t)
            last_t = action.t_s
            out.append((planned, gap if gap > 0 else SETTLE_INTERVAL_S))
        return out

    async def _run_closed(self, scenario: Scenario, plan: WorkloadPlan) -> LoadReport:
        assert plan.closed_model is not None
        report = LoadReport()
        users = plan.closed_model.users
        await asyncio.gather(
            *(
                self._run_closed_user(
                    scenario, report, user_index=i, duration_s=plan.duration_s
                )
                for i in range(users)
            )
        )
        return report

    # -- open model ---------------------------------------------------------- #

    async def _run_open(self, scenario: Scenario, plan: WorkloadPlan) -> LoadReport:
        assert plan.open_model is not None
        report = LoadReport()
        arrivals = plan.open_model.arrival_times(duration_s=plan.duration_s)
        # A small shared session pool the arrivals issue intents against.
        pool_size = max(1, min(len(arrivals), 64))
        sessions = [
            scenario.session(
                session_id=f"sess_open_{i:05d}",
                book_id=self._config.book_id,
                seed=self._config.seed + i,
            )
            for i in range(pool_size)
        ]
        # Open the pooled sessions once (prologue), counted in the report.
        for session in sessions:
            await self._issue(report, session.prologue())

        # Fire one request per arrival, sleeping the inter-arrival gap. Each
        # arrival's request is issued without awaiting completion serially is
        # acceptable here because the transport is the unit under test; for real
        # open load the CLI uses a bounded task set (see note below).
        start = self._clock()
        prev = 0.0
        pending: list[asyncio.Task[Response]] = []
        for i, t in enumerate(arrivals):
            gap = max(0.0, t - prev)
            prev = t
            if gap > 0.0:
                await self._sleep(gap)
            if self._clock() - start >= plan.duration_s:
                break
            session = sessions[i % pool_size]
            planned = next(session.requests(duration_s=plan.duration_s), None)
            if planned is None:
                planned = session.prologue()
            pending.append(asyncio.create_task(self._issue(report, planned)))
        if pending:
            await asyncio.gather(*pending)
        return report

    # -- entrypoint ---------------------------------------------------------- #

    async def run(self, scenario: Scenario, plan: WorkloadPlan) -> LoadReport:
        """Run ``scenario`` under ``plan`` and return the populated report."""
        start = self._clock()
        if plan.kind is WorkloadKind.CLOSED:
            report = await self._run_closed(scenario, plan)
        else:
            report = await self._run_open(scenario, plan)
        report.wall_seconds = max(0.0, self._clock() - start)
        report.meta.setdefault("scenario", scenario.name)
        report.meta.setdefault("workload", plan.describe())
        return report


class VirtualClock:
    """A deterministic monotonic clock advanced by an instant async sleep.

    Lets the runner be driven in tests with zero real waiting: ``sleep(dt)``
    advances ``now()`` by ``dt`` immediately, so think-time pacing and the run
    duration are honoured in *model* time without any wall-clock delay.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        """The current virtual time (seconds)."""
        return self._t

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` (no real waiting)."""
        self._t += max(0.0, seconds)
        # Yield to the loop so concurrent tasks interleave deterministically.
        await asyncio.sleep(0)


__all__ = [
    "ClockFn",
    "LoadRunner",
    "RunnerConfig",
    "SleepFn",
    "VirtualClock",
]
