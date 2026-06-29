"""Multi-provider fan-out with first-good-wins racing and hard cost caps.

When one logical generation can be served by several providers (the primary
Qwen tier, a cheaper fallback, a third vendor) the gateway can *race* them and
take the first answer that passes validation — trading a little extra spend for
tail-latency robustness. This module orchestrates that race with three controls
that keep it from becoming a money pit:

* **Cost cap.** Each candidate declares a ``cost`` (abstract units — tokens,
  cents, video-seconds, §11). The orchestrator only *starts* a candidate if the
  cost already committed plus its cost stays within ``cost_cap``. A race that
  cannot even start its cheapest candidate raises
  :class:`~app.inference.accel.errors.CostCapExceededError`.
* **Hedging delay.** Candidates need not all start at once. ``hedge_delay`` staggers
  launches: the next provider is only started if the leaders have not produced a
  good answer within the delay. The common case (primary answers fast) then only
  ever pays for the primary. Delays are measured on the injected clock, so tests
  are deterministic.
* **Validator.** A good answer is one a ``validate`` callback accepts (e.g. the
  output parses as the required JSON, is non-empty, passes a guardrail). A
  candidate that returns an *invalid* answer counts as a failure, not a win, and
  the race continues to the next.

The first valid answer cancels the still-running candidates. Concurrency is real
``asyncio`` here (this is the one place the accel layer schedules tasks); the
deterministic clock + ``asyncio.Event`` make the staggering reproducible without
wall-clock sleeps in tests (a test supplies a clock whose ``sleep`` resolves on
event signalling).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from .clock import SYSTEM_CLOCK, Clock
from .errors import CostCapExceededError, FanOutExhaustedError
from .metrics import FanOutMetrics
from .protocol import GenerationRequest, GenerationResult

#: A candidate's generate coroutine factory.
GenerateFn = Callable[[GenerationRequest], Awaitable[GenerationResult]]
#: Validates a candidate's answer (True = acceptable, ends the race).
Validator = Callable[[GenerationResult], bool]


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """One racer in a fan-out.

    Attributes:
        name: Stable id for telemetry / win attribution.
        generate: The coroutine factory that produces an answer for a request.
        cost: Abstract cost units charged when this candidate is *started*.
        priority: Lower starts earlier; ties keep declaration order. The primary
            provider is priority 0.
    """

    name: str
    generate: GenerateFn
    cost: float = 1.0
    priority: int = 0


@dataclass(slots=True)
class FanOutResult:
    """The outcome of a race."""

    result: GenerationResult
    winner: str
    started: list[str] = field(default_factory=list)
    cost_spent: float = 0.0
    losers_cancelled: int = 0


def _default_validator(result: GenerationResult) -> bool:
    """Accept any non-empty answer."""
    return bool(result.text) or bool(result.tokens)


async def _default_sleep(clock: Clock, seconds: float, cancel: asyncio.Event) -> None:
    """Sleep that wakes early if ``cancel`` is set; uses real time by default.

    Tests inject a clock whose ``sleep`` is event-driven; the production default
    uses ``asyncio.wait_for`` so the hedge delay is real but interruptible.
    """
    sleeper = getattr(clock, "sleep", None)
    if callable(sleeper):
        await sleeper(seconds, cancel)
        return
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except TimeoutError:
        return


class FanOutRacer:
    """Races provider candidates first-good-wins under a cost cap + hedging."""

    def __init__(
        self,
        *,
        cost_cap: float = float("inf"),
        hedge_delay: float = 0.0,
        validate: Validator | None = None,
        metrics: FanOutMetrics | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if cost_cap <= 0:
            raise ValueError("cost_cap must be positive")
        self._cost_cap = cost_cap
        self._hedge_delay = max(0.0, hedge_delay)
        self._validate = validate or _default_validator
        self._metrics = metrics or FanOutMetrics()
        self._clock = clock

    @property
    def metrics(self) -> FanOutMetrics:
        return self._metrics

    async def race(
        self,
        request: GenerationRequest,
        candidates: Sequence[ProviderCandidate],
    ) -> FanOutResult:
        """Race ``candidates`` for ``request``; return the first valid answer.

        Raises:
            CostCapExceededError: the cheapest candidate alone exceeds the cap.
            FanOutExhaustedError: every started candidate failed or was vetoed.
        """
        ordered = sorted(candidates, key=lambda c: c.priority)
        if not ordered:
            raise FanOutExhaustedError("no candidates supplied")

        cheapest = min(c.cost for c in ordered)
        if cheapest > self._cost_cap:
            self._metrics.record_cap_rejection()
            raise CostCapExceededError(
                "cheapest candidate exceeds cost cap",
                cost_cap=self._cost_cap,
                would_spend=cheapest,
            )

        done = asyncio.Event()
        cost_spent = 0.0
        started: list[str] = []
        failures: list[BaseException] = []
        winner: tuple[str, GenerationResult] | None = None
        tasks: list[asyncio.Task[None]] = []

        async def run_candidate(cand: ProviderCandidate) -> None:
            nonlocal winner
            try:
                result = await cand.generate(request)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - a racer fault must not kill the race
                failures.append(exc)
                return
            if self._validate(result) and winner is None:
                winner = (cand.name, result.with_meta(provider=cand.name, accelerator="fanout"))
                done.set()
            elif winner is None:
                failures.append(
                    FanOutExhaustedError(f"candidate {cand.name!r} produced an invalid answer")
                )

        idx = 0
        while idx < len(ordered) and winner is None:
            cand = ordered[idx]
            # Admit only if it keeps us within the cap.
            if cost_spent + cand.cost > self._cost_cap:
                break
            cost_spent += cand.cost
            started.append(cand.name)
            tasks.append(asyncio.ensure_future(run_candidate(cand)))
            idx += 1

            if idx >= len(ordered):
                break
            # Hedge: wait up to hedge_delay for a winner before launching the next.
            await _default_sleep(self._clock, self._hedge_delay, done)
            if done.is_set():
                break

        # Wait for either a winner or all started candidates to finish.
        await self._await_completion(tasks, done)

        cancelled = await self._cancel_pending(tasks)

        if winner is None:
            self._metrics.record_race(
                started=len(started),
                cancelled=cancelled,
                won=False,
                failures=len(failures),
                cost=cost_spent,
            )
            raise FanOutExhaustedError(
                f"all {len(started)} fan-out candidate(s) failed",
                attempts=len(started),
                cost_spent=cost_spent,
                last_error=failures[-1] if failures else None,
            )

        self._metrics.record_race(
            started=len(started),
            cancelled=cancelled,
            won=True,
            failures=len(failures),
            cost=cost_spent,
        )
        name, result = winner
        return FanOutResult(
            result=result,
            winner=name,
            started=started,
            cost_spent=cost_spent,
            losers_cancelled=cancelled,
        )

    @staticmethod
    async def _await_completion(
        tasks: list[asyncio.Task[None]], done: asyncio.Event
    ) -> None:
        """Wait until a winner is signalled or every task has finished."""
        if not tasks:
            return
        pending = set(tasks)
        while pending and not done.is_set():
            finished, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            # loop re-checks done; if a winner fired, stop waiting on the rest.

    @staticmethod
    async def _cancel_pending(tasks: list[asyncio.Task[None]]) -> int:
        cancelled = 0
        for t in tasks:
            if not t.done():
                t.cancel()
                cancelled += 1
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return cancelled


async def first_good(
    request: GenerationRequest,
    candidates: Sequence[ProviderCandidate],
    *,
    cost_cap: float = float("inf"),
    hedge_delay: float = 0.0,
    validate: Validator | None = None,
    metrics: FanOutMetrics | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> FanOutResult:
    """One-shot convenience wrapper around :class:`FanOutRacer`."""
    racer = FanOutRacer(
        cost_cap=cost_cap,
        hedge_delay=hedge_delay,
        validate=validate,
        metrics=metrics,
        clock=clock,
    )
    return await racer.race(request, candidates)


__all__ = [
    "FanOutRacer",
    "FanOutResult",
    "GenerateFn",
    "ProviderCandidate",
    "Validator",
    "first_good",
]
