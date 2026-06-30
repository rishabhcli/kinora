"""The load generator — drives a target under closed- or open-loop load (§12.2).

This is the orchestrator. Given an injected :class:`~app.loadtest.clock.Clock`, a
:class:`~app.loadtest.target.Target`, a :class:`~app.loadtest.scenario.Scenario`,
and a :class:`LoadPlan`, it issues requests and feeds every completion to a
:class:`~app.loadtest.collector.LatencyCollector`.

Two load models, the classic open-vs-closed distinction:

* **Closed loop** — a fixed population of ``N`` virtual readers, each looping the
  scenario journey *request → think → request*. Concurrency is capped at ``N``;
  if the server slows, readers simply wait, so offered load self-throttles. This
  is the natural Kinora shape (a fixed set of readers each driving one session).
  Latency here is genuinely user-perceived because the reader *is* the next
  request's gate — there is no coordinated-omission risk in a pure closed loop,
  but we still record against intended time for a uniform code path.

* **Open loop** — requests arrive on the precomputed
  :func:`~app.loadtest.arrival.make_schedule` at the target rate, *independent* of
  how fast the target responds. Each arrival is dispatched as its own task at its
  intended time; the collector measures from intended time and backfills omitted
  slots during stalls. This is the model that exposes backpressure and queueing
  collapse. Endpoint selection follows the scenario's endpoint mix.

Both run under the *same* clock seam, so a :class:`VirtualClock` makes the whole
thing deterministic and instant in tests, while a :class:`WallClock` runs it for
real. The result is a :class:`RunResult` carrying the populated collector + the
plan, which the report / budget / regression layers consume.

The generator never enables live video, never touches infra, and spends nothing
— it only calls ``target.send``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from app.loadtest.arrival import RateEnvelope, make_schedule
from app.loadtest.clock import Clock, WallClock
from app.loadtest.collector import LatencyCollector
from app.loadtest.scenario import Scenario
from app.loadtest.target import LoadRequest, Outcome, Target


class LoadModel(StrEnum):
    """Which load model the run uses."""

    CLOSED = "closed"  # N looping virtual readers
    OPEN = "open"  # arrivals at a target rate, independent of response time


@dataclass(frozen=True, slots=True)
class LoadPlan:
    """A fully-specified run: model, scale, scenario binding, and timing knobs."""

    model: LoadModel
    scenario: Scenario
    #: CLOSED: number of concurrent virtual readers. Ignored for OPEN.
    users: int = 10
    #: CLOSED: how many times each reader repeats the journey.
    iterations: int = 1
    #: OPEN: the arrival-rate envelope (required for OPEN).
    envelope: RateEnvelope | None = None
    #: OPEN: bursty Poisson arrivals (True) vs. evenly paced (False).
    poisson: bool = True
    #: Per-request deadline (seconds); ``None`` = no timeout enforced here.
    timeout_s: float | None = None
    #: Apply coordinated-omission correction in the collector.
    correct_omission: bool = True
    #: Master seed for all RNG (think times, arrivals, target jitter).
    seed: int = 1234

    def __post_init__(self) -> None:
        if self.model is LoadModel.CLOSED:
            if self.users < 1:
                raise ValueError("CLOSED load requires users >= 1")
            if self.iterations < 1:
                raise ValueError("CLOSED load requires iterations >= 1")
        else:
            if self.envelope is None:
                raise ValueError("OPEN load requires an arrival envelope")


@dataclass(slots=True)
class RunResult:
    """The output of a run: the populated collector + run metadata."""

    plan: LoadPlan
    collector: LatencyCollector
    #: Total requests the generator attempted to send (incl. dropped).
    attempted: int = 0
    #: Requests dropped before sending (open-loop backpressure / max-inflight).
    dropped: int = 0


class LoadGenerator:
    """Drives a :class:`Target` per a :class:`LoadPlan` under an injected clock."""

    __slots__ = ("_clock", "_target", "_max_inflight")

    def __init__(
        self,
        clock: Clock | None = None,
        target: Target | None = None,
        *,
        max_inflight: int | None = None,
    ) -> None:
        if target is None:
            raise ValueError("LoadGenerator requires a target")
        self._clock = clock or WallClock()
        self._target = target
        #: Open-loop backpressure: cap simultaneous in-flight requests; arrivals
        #: over the cap are *dropped* (not queued unbounded) — the §12.2 shape.
        self._max_inflight = max_inflight

    async def run(self, plan: LoadPlan) -> RunResult:
        collector = LatencyCollector(correct_omission=plan.correct_omission)
        result = RunResult(plan=plan, collector=collector)
        if plan.model is LoadModel.CLOSED:
            await self._run_closed(plan, result)
        else:
            await self._run_open(plan, result)
        return result

    # ----- closed loop ---------------------------------------------------- #

    async def _run_closed(self, plan: LoadPlan, result: RunResult) -> None:
        import asyncio

        async def reader(user_index: int) -> None:
            rng = random.Random((plan.seed << 16) ^ user_index)
            for it in range(plan.iterations):
                session_id = f"u{user_index}-it{it}"
                for request, think_after in plan.scenario.expand(rng, session_id=session_id):
                    result.attempted += 1
                    await self._send_and_record(
                        request, result, intended_s=self._clock.now()
                    )
                    if think_after > 0:
                        await self._clock.sleep(think_after)

        await asyncio.gather(*(reader(i) for i in range(plan.users)))

    # ----- open loop ------------------------------------------------------ #

    async def _run_open(self, plan: LoadPlan, result: RunResult) -> None:
        import asyncio

        assert plan.envelope is not None  # guarded by LoadPlan.__post_init__
        sched_rng = random.Random(plan.seed)
        schedule = make_schedule(plan.envelope, poisson=plan.poisson, rng=sched_rng)

        mix = plan.scenario.endpoint_mix()
        endpoints = list(mix)
        weights = [mix[e] for e in endpoints]
        pick_rng = random.Random(plan.seed ^ 0xABCDEF)

        # The expected inter-arrival interval the collector uses for omission
        # backfill: the run-average gap = duration / count.
        expected_interval = (
            plan.envelope.duration_s / len(schedule) if schedule else None
        )

        inflight: set[asyncio.Task[None]] = set()
        start = self._clock.now()

        async def fire(intended_abs: float, endpoint: str) -> None:
            request = LoadRequest(endpoint=endpoint, payload={}, timeout_s=plan.timeout_s)
            await self._send_and_record(
                request, result, intended_s=intended_abs, expected_interval_s=expected_interval
            )

        for arrival_t in schedule:
            target_time = start + arrival_t
            wait = target_time - self._clock.now()
            if wait > 0:
                await self._clock.sleep(wait)
            result.attempted += 1
            endpoint = (
                pick_rng.choices(endpoints, weights=weights, k=1)[0]
                if endpoints
                else plan.scenario.steps[0].endpoint
            )
            # Backpressure: drop if we are already at the in-flight cap.
            if self._max_inflight is not None and len(inflight) >= self._max_inflight:
                result.dropped += 1
                result.collector.record_dropped(endpoint, intended_s=target_time)
                continue
            task = asyncio.ensure_future(fire(target_time, endpoint))
            inflight.add(task)
            task.add_done_callback(inflight.discard)

        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)

    # ----- shared send path ---------------------------------------------- #

    async def _send_and_record(
        self,
        request: LoadRequest,
        result: RunResult,
        *,
        intended_s: float,
        expected_interval_s: float | None = None,
    ) -> None:
        response = await self._target.send(request)
        finish_s = self._clock.now()
        # Honour a deadline: reclassify an over-deadline OK as TIMEOUT.
        deadline = request.timeout_s if request.timeout_s is not None else result.plan.timeout_s
        if deadline is not None and (finish_s - intended_s) > deadline and response.ok:
            from app.loadtest.target import LoadResponse

            response = LoadResponse(
                endpoint=response.endpoint,
                outcome=Outcome.TIMEOUT,
                latency_s=response.latency_s,
                status=response.status,
            )
        result.collector.record(
            response,
            intended_s=intended_s,
            finish_s=finish_s,
            expected_interval_s=expected_interval_s,
        )
