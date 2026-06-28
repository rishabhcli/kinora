"""Fault-injection + chaos primitives for testing the gateway.

Production resilience is only real if you can *prove* it under failure. This module
supplies the deterministic chaos toolkit the gateway's test suite (and any future
soak harness) builds on:

* :class:`FaultPlan` / :class:`FaultProfile` — a scripted sequence of outcomes
  (ok / timeout / 429 / 5xx / connection-reset / latency) keyed to attempt number
  or to a probabilistic seed. No real network, no real clock.
* :class:`ChaosTransport` — an :class:`httpx.MockTransport` driver that turns a
  :class:`FaultProfile` into HTTP responses / exceptions, so the round-1
  :class:`~app.providers.base.ProviderClient` exercises its real parsing + retry
  paths against scripted faults.
* :class:`FakeClock` / :func:`make_async_sleep` — a controllable monotonic clock
  and an async sleep that advances it, so a gateway retry loop with multi-second
  backoff completes instantly and deterministically in a test.
* :class:`ChaoticAttempt` — a tiny async callable that raises a scripted sequence
  of typed :class:`~app.providers.errors.ProviderError` s, for unit-testing the
  gateway's loop without any HTTP at all.

Everything here is import-safe and pulls in nothing heavy; it lives in ``app`` (not
``tests``) so other resilience modules or a CLI soak tool can reuse it.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from ..errors import (
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)


class FaultKind(StrEnum):
    """The outcome of one scripted attempt."""

    OK = "ok"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"  # 429
    SERVER_ERROR = "server_error"  # 5xx
    CONNECTION_RESET = "connection_reset"  # transport disconnect
    BAD_REQUEST = "bad_request"  # 4xx (non-retryable)


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """A scripted, deterministic sequence of attempt outcomes.

    ``sequence[i]`` is the outcome of the (i+1)-th attempt; once the sequence is
    exhausted, ``terminal`` repeats forever (defaults to OK so "fail N then
    recover" is the natural shape).
    """

    sequence: tuple[FaultKind, ...]
    terminal: FaultKind = FaultKind.OK
    #: Optional per-attempt latency seconds (parallel to ``sequence``); the chaos
    #: tooling can surface this to a fake sleep for ordering tests.
    latencies: tuple[float, ...] = ()
    #: ``Retry-After`` seconds attached to a RATE_LIMIT outcome (server hint).
    retry_after_s: float | None = None

    def kind_for(self, attempt: int) -> FaultKind:
        """The outcome for a 1-based attempt number."""
        idx = attempt - 1
        if 0 <= idx < len(self.sequence):
            return self.sequence[idx]
        return self.terminal

    def latency_for(self, attempt: int) -> float:
        idx = attempt - 1
        if 0 <= idx < len(self.latencies):
            return self.latencies[idx]
        return 0.0


@dataclass
class FaultProfile:
    """A stateful fault driver: either a scripted :class:`FaultPlan` or random.

    With a ``plan`` it is fully deterministic (walks the sequence). With a
    ``probabilities`` map + ``rng`` it samples an outcome per attempt — useful for a
    soak/chaos run where you want statistical, not scripted, failures.
    """

    plan: FaultPlan | None = None
    probabilities: dict[FaultKind, float] = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)
    _attempt: int = 0

    def next_kind(self) -> tuple[FaultKind, float, float | None]:
        """Advance one attempt; return ``(kind, latency_s, retry_after_s)``."""
        self._attempt += 1
        if self.plan is not None:
            kind = self.plan.kind_for(self._attempt)
            latency = self.plan.latency_for(self._attempt)
            retry_after = self.plan.retry_after_s if kind is FaultKind.RATE_LIMIT else None
            return kind, latency, retry_after
        kind = self._sample()
        return kind, 0.0, None

    def _sample(self) -> FaultKind:
        if not self.probabilities:
            return FaultKind.OK
        roll = self.rng.random()
        cumulative = 0.0
        for kind, prob in self.probabilities.items():
            cumulative += prob
            if roll < cumulative:
                return kind
        return FaultKind.OK

    @property
    def attempts(self) -> int:
        return self._attempt

    def reset(self) -> None:
        self._attempt = 0


def _ok_chat_body(model: str) -> dict[str, object]:
    """A minimal OpenAI-compatible chat success body the round-1 parser accepts."""
    return {
        "model": model,
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class ChaosTransport:
    """An :class:`httpx.MockTransport` factory driven by a :class:`FaultProfile`.

    Drop the produced transport into ``ProviderClient(transport=...)`` to exercise
    the real HTTP parse/retry path against scripted faults. ``ok_body`` lets a test
    override the success body shape (default: a chat completion).
    """

    def __init__(
        self,
        profile: FaultProfile,
        *,
        model: str = "test-model",
        ok_body: Callable[[str], dict[str, object]] | None = None,
    ) -> None:
        self.profile = profile
        self.model = model
        self._ok_body = ok_body or _ok_chat_body
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        kind, _latency, retry_after = self.profile.next_kind()
        if kind is FaultKind.OK:
            return httpx.Response(200, json=self._ok_body(self.model))
        if kind is FaultKind.TIMEOUT:
            raise httpx.ReadTimeout("chaos: read timeout", request=request)
        if kind is FaultKind.CONNECTION_RESET:
            raise httpx.ConnectError("chaos: connection reset", request=request)
        if kind is FaultKind.RATE_LIMIT:
            headers = {}
            if retry_after is not None:
                headers["Retry-After"] = str(int(retry_after))
            return httpx.Response(
                429,
                headers=headers,
                json={"error": {"code": "Throttling.RateQuota", "message": "chaos: throttled"}},
            )
        if kind is FaultKind.SERVER_ERROR:
            return httpx.Response(
                503, json={"error": {"code": "InternalError", "message": "chaos: 5xx"}}
            )
        # BAD_REQUEST
        return httpx.Response(
            400, json={"error": {"code": "InvalidParameter", "message": "chaos: bad request"}}
        )


@dataclass
class FakeClock:
    """A controllable monotonic clock for deterministic time-based tests."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_async_sleep(clock: FakeClock) -> Callable[[float], Awaitable[None]]:
    """An async sleep that advances ``clock`` instead of waiting on the wall clock.

    It still yields control to the event loop once (``asyncio.sleep(0)``) so that
    concurrent tasks make progress and a tight ``acquire`` retry loop can't starve
    them — the time itself is virtual, but cooperative scheduling is preserved.
    """

    import asyncio

    async def _sleep(seconds: float) -> None:
        clock.advance(max(seconds, 0.0))
        await asyncio.sleep(0)

    return _sleep


class ChaoticAttempt:
    """A scripted async attempt that raises typed errors then succeeds.

    Build with a :class:`FaultProfile`; each ``await attempt()`` advances it and
    either returns ``result`` (OK) or raises the matching
    :class:`~app.providers.errors.ProviderError`. The gateway loop can be tested
    end-to-end against this with no HTTP at all.
    """

    def __init__(self, profile: FaultProfile, *, result: object = "ok") -> None:
        self.profile = profile
        self.result = result
        self.invocations = 0

    async def __call__(self) -> object:
        self.invocations += 1
        kind, _latency, retry_after = self.profile.next_kind()
        if kind is FaultKind.OK:
            return self.result
        raise _error_for(kind, retry_after)


def _error_for(kind: FaultKind, retry_after_s: float | None) -> ProviderError:
    if kind is FaultKind.TIMEOUT:
        return ProviderTimeout("chaos: timeout")
    if kind is FaultKind.RATE_LIMIT:
        return RateLimited("chaos: throttled", retry_after_s=retry_after_s, status_code=429)
    if kind is FaultKind.SERVER_ERROR:
        return TransientProviderError("chaos: 5xx", status_code=503)
    if kind is FaultKind.CONNECTION_RESET:
        return TransientProviderError("chaos: connection reset")
    return ProviderBadRequest("chaos: bad request", status_code=400)


__all__ = [
    "ChaosTransport",
    "ChaoticAttempt",
    "FakeClock",
    "FaultKind",
    "FaultPlan",
    "FaultProfile",
    "make_async_sleep",
]
