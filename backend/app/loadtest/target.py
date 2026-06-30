"""The target seam — what a request is sent *to* (real HTTP or an in-process fake).

The load generator must never depend on a concrete transport. It depends on the
:class:`Target` protocol: *given a* :class:`LoadRequest`, *await a*
:class:`LoadResponse`. This single seam is what lets the same generator drive

* a real Kinora API over HTTP in a production run, and
* a deterministic in-process **fake** under a :class:`~app.loadtest.clock.VirtualClock`
  in tests — no sockets, no network, fully reproducible.

The task spec is explicit: *the harness drives an injectable in-process target,
NOT real HTTP, in tests.* So the fake here is first-class, not an afterthought.

A :class:`LoadResponse` carries everything the collector needs: the
service-perceived latency, an ``ok`` flag, and a small status/category so reports
can split success from error. Latency is reported by the target (the fake derives
it from the clock and a configurable service-time model); the *collector* is what
adds the queueing/coordinated-omission correction on top, from the intended send
time — see :mod:`app.loadtest.collector`.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.loadtest.clock import Clock


class Outcome(StrEnum):
    """Coarse request outcome, for report bucketing."""

    OK = "ok"
    ERROR = "error"  # the target returned a failure status
    TIMEOUT = "timeout"  # the request exceeded its deadline
    DROPPED = "dropped"  # backpressure dropped it before it ran (never sent)


@dataclass(frozen=True, slots=True)
class LoadRequest:
    """One logical request the harness will issue against the target.

    ``endpoint`` is the *logical* endpoint name (e.g. ``"open_book"``,
    ``"page_turn"``), used to key per-endpoint latency budgets and the report's
    per-endpoint breakdown — it is intentionally decoupled from any URL path so
    budgets survive routing changes. ``payload`` is opaque to the harness and
    passed straight to the target.
    """

    endpoint: str
    payload: Mapping[str, object] = field(default_factory=dict)
    #: Optional per-request deadline (seconds). ``None`` means use the run's.
    timeout_s: float | None = None


@dataclass(frozen=True, slots=True)
class LoadResponse:
    """The target's answer: outcome + the service-perceived latency."""

    endpoint: str
    outcome: Outcome
    #: Service latency in seconds (time the target spent answering).
    latency_s: float
    #: Numeric status (HTTP-like; 0 when not applicable, e.g. a drop).
    status: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@runtime_checkable
class Target(Protocol):
    """The async target seam the generator depends on."""

    async def send(self, request: LoadRequest) -> LoadResponse:
        """Issue ``request`` and await its :class:`LoadResponse`."""
        ...

    async def aclose(self) -> None:
        """Release any resources (no-op for the fake)."""
        ...


#: A per-endpoint service-time model: ``(endpoint, rng) -> latency_seconds``.
ServiceTime = Callable[[str, random.Random], float]


def constant_service_time(seconds: float) -> ServiceTime:
    """A degenerate model: every endpoint takes exactly ``seconds``."""

    def _model(_endpoint: str, _rng: random.Random) -> float:
        return seconds

    return _model


def lognormal_service_time(
    median_s: float, sigma: float = 0.5, *, floor_s: float = 0.0
) -> ServiceTime:
    """A realistic latency model: log-normal around ``median_s``.

    Latency is heavy-tailed in practice (most fast, a long tail of slow), which
    is exactly what a percentile-aware harness must exercise. ``median_s`` is the
    distribution's median (``exp(mu)``); ``sigma`` controls tail weight.
    """
    import math

    mu = math.log(max(median_s, 1e-9))

    def _model(_endpoint: str, rng: random.Random) -> float:
        return max(floor_s, rng.lognormvariate(mu, sigma))

    return _model


def per_endpoint_service_time(
    table: Mapping[str, ServiceTime], *, default: ServiceTime | None = None
) -> ServiceTime:
    """Route each endpoint to its own service-time model (with a fallback)."""
    fallback = default or constant_service_time(0.0)

    def _model(endpoint: str, rng: random.Random) -> float:
        return table.get(endpoint, fallback)(endpoint, rng)

    return _model


class FakeTarget:
    """A deterministic, in-process target driven by an injected clock + RNG.

    On :meth:`send` it (1) draws a service time from the configured model, (2)
    sleeps that long on the *clock* (so a :class:`VirtualClock` advances exactly
    by the modelled latency, with no real time spent), and (3) returns a
    :class:`LoadResponse`. An optional ``error_rate`` and ``failing_endpoints``
    let tests exercise the error-bucketing and budget-gate paths.

    A bounded ``concurrency`` models a server with a finite worker pool: when all
    workers are busy, an arriving request *queues* (waits on the clock), which is
    precisely the condition that creates coordinated omission — and lets the
    collector's correction be tested against a known queue.
    """

    __slots__ = (
        "_clock",
        "_concurrency",
        "_error_rate",
        "_failing",
        "_rng",
        "_sem",
        "_sem_ready",
        "_service_time",
        "sent_count",
    )

    def __init__(
        self,
        clock: Clock,
        service_time: ServiceTime,
        *,
        seed: int = 0,
        error_rate: float = 0.0,
        failing_endpoints: frozenset[str] | None = None,
        concurrency: int | None = None,
    ) -> None:
        self._clock = clock
        self._service_time = service_time
        self._rng = random.Random(seed)
        self._error_rate = error_rate
        self._failing = failing_endpoints or frozenset()
        self._concurrency = concurrency
        # The semaphore must bind to the running loop, so it is created lazily on
        # first send (FakeTarget can be constructed outside an event loop).
        self._sem: object | None = None
        self._sem_ready = False
        self.sent_count = 0

    def _semaphore(self) -> object | None:
        import asyncio

        if not self._sem_ready:
            self._sem = (
                asyncio.Semaphore(self._concurrency) if self._concurrency else None
            )
            self._sem_ready = True
        return self._sem

    async def send(self, request: LoadRequest) -> LoadResponse:
        sem = self._semaphore()
        if sem is not None:
            await sem.acquire()  # type: ignore[attr-defined]
        try:
            self.sent_count += 1
            service = max(0.0, self._service_time(request.endpoint, self._rng))
            start = self._clock.now()
            await self._clock.sleep(service)
            latency = self._clock.now() - start
            fails = request.endpoint in self._failing or (
                self._error_rate > 0.0 and self._rng.random() < self._error_rate
            )
            outcome = Outcome.ERROR if fails else Outcome.OK
            status = 500 if fails else 200
            return LoadResponse(
                endpoint=request.endpoint,
                outcome=outcome,
                latency_s=latency,
                status=status,
            )
        finally:
            if sem is not None:
                sem.release()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        return None


class CallableTarget:
    """Adapt a plain async ``callable(LoadRequest) -> LoadResponse`` to a Target.

    Convenience for tests that want to script per-request behaviour without a
    full :class:`FakeTarget`.
    """

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[LoadRequest], Awaitable[LoadResponse]]) -> None:
        self._fn = fn

    async def send(self, request: LoadRequest) -> LoadResponse:
        return await self._fn(request)

    async def aclose(self) -> None:
        return None
