"""The circuit breaker — stop hammering an endpoint that is clearly down.

When an endpoint is failing, continuing to call it wastes the caller's deadline,
piles load onto a sick service, and delays the fallback the caller could be
running instead (the degradation ladder §12.4). The breaker is the standard
three-state machine that converts "this endpoint is unhealthy" into a *fast*,
*local* ``UNAVAILABLE`` so the caller fails over immediately:

* **CLOSED** — calls flow. Outcomes feed a rolling window; once the failure ratio
  over a minimum sample crosses ``failure_threshold``, the breaker **opens**.
* **OPEN** — calls are rejected instantly with a transport ``UNAVAILABLE`` (no
  attempt made). After ``reset_timeout_s`` it moves to half-open to probe.
* **HALF_OPEN** — a small number of *trial* calls are admitted. Enough
  consecutive successes **close** it (recovered); a single failure **re-opens**
  it (still sick) with the timeout reset.

Only **transport-kind** failures (and configured retryable statuses) count
against the breaker: an application ``NOT_FOUND`` from a healthy server must not
trip it. The window is a fixed-size ring of booleans, and every time decision
goes through the injected :class:`Clock`, so the breaker is fully deterministic
under a :class:`ManualClock`.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field

from app.distributed.rpc.deadline import Clock
from app.distributed.rpc.errors import FailureKind, RpcError, RpcStatus


class BreakerState(enum.Enum):
    """The breaker's state machine."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    """Tuning for one :class:`CircuitBreaker`.

    ``failure_threshold`` is the failure *ratio* (0–1) over the rolling window
    that opens the breaker; ``min_samples`` gates that ratio so a single early
    failure can't open it on cold start. ``window_size`` is how many recent
    outcomes are weighed. ``reset_timeout_s`` is the open→half-open probe delay.
    ``half_open_max_calls`` trial calls are admitted while half-open, and
    ``half_open_successes`` consecutive successes are needed to close.
    """

    failure_threshold: float = 0.5
    min_samples: int = 5
    window_size: int = 20
    reset_timeout_s: float = 5.0
    half_open_max_calls: int = 3
    half_open_successes: int = 2


@dataclass
class CircuitBreaker:
    """A per-endpoint three-state circuit breaker.

    Drive it with :meth:`allow` (before a call) and :meth:`record` (after). The
    client uses :meth:`guard` to both check admission and short-circuit with a
    fast ``UNAVAILABLE`` when open.
    """

    config: CircuitConfig = field(default_factory=CircuitConfig)
    name: str = "circuit"
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _window: deque[bool] = field(default_factory=deque, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _half_open_in_flight: int = field(default=0, init=False)
    _half_open_successes: int = field(default=0, init=False)
    #: Transition counters (observability).
    opened_count: int = field(default=0, init=False)
    rejected_count: int = field(default=0, init=False)

    @property
    def state(self) -> BreakerState:
        """The current state (read-only; advances via :meth:`allow`/:meth:`record`)."""
        return self._state

    def _maybe_half_open(self, *, clock: Clock) -> None:
        """Promote OPEN → HALF_OPEN once the reset timeout has elapsed."""
        if (
            self._state is BreakerState.OPEN
            and clock.now() - self._opened_at >= self.config.reset_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_in_flight = 0
            self._half_open_successes = 0

    def allow(self, *, clock: Clock) -> bool:
        """Whether a call may proceed *now*. Reserves a half-open trial slot.

        In CLOSED, always allows. In OPEN, allows only after the timeout (which
        flips it to HALF_OPEN). In HALF_OPEN, allows up to ``half_open_max_calls``
        concurrent trial calls, rejecting the rest.
        """
        self._maybe_half_open(clock=clock)
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            self.rejected_count += 1
            return False
        # HALF_OPEN: admit a bounded number of probes.
        if self._half_open_in_flight < self.config.half_open_max_calls:
            self._half_open_in_flight += 1
            return True
        self.rejected_count += 1
        return False

    def record(self, *, success: bool, clock: Clock) -> None:
        """Record a call outcome and advance the state machine."""
        if self._state is BreakerState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            if success:
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_successes:
                    self._close()
            else:
                self._open(clock=clock)
            return

        # CLOSED: feed the rolling window and re-evaluate.
        self._window.append(success)
        while len(self._window) > self.config.window_size:
            self._window.popleft()
        if self._state is BreakerState.CLOSED and self._should_open():
            self._open(clock=clock)

    def _should_open(self) -> bool:
        """Whether the rolling failure ratio crosses the threshold."""
        if len(self._window) < self.config.min_samples:
            return False
        failures = sum(1 for ok in self._window if not ok)
        ratio = failures / len(self._window)
        return ratio >= self.config.failure_threshold

    def _open(self, *, clock: Clock) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = clock.now()
        self._half_open_in_flight = 0
        self._half_open_successes = 0
        self.opened_count += 1

    def _close(self) -> None:
        self._state = BreakerState.CLOSED
        self._window.clear()
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    def counts_against_breaker(self, error: RpcError) -> bool:
        """Whether an error should be recorded as a breaker failure.

        Only transport-kind failures (and ``RESOURCE_EXHAUSTED`` / ``UNAVAILABLE``
        / ``DEADLINE_EXCEEDED``, which signal endpoint distress) count. An
        application client error (``NOT_FOUND``, ``INVALID_ARGUMENT``) from a
        healthy server is a *success* from the breaker's perspective.
        """
        if error.kind is FailureKind.TRANSPORT:
            return True
        return error.status in {
            RpcStatus.UNAVAILABLE,
            RpcStatus.RESOURCE_EXHAUSTED,
            RpcStatus.DEADLINE_EXCEEDED,
            RpcStatus.INTERNAL,
        }

    def reject_error(self) -> RpcError:
        """The fast-fail error raised when the breaker rejects a call."""
        return RpcError(
            RpcStatus.UNAVAILABLE,
            f"circuit breaker open for {self.name}",
            kind=FailureKind.TRANSPORT,
        )


@dataclass
class CircuitBreakerRegistry:
    """Per-endpoint breakers, lazily created from a shared default config."""

    default_config: CircuitConfig = field(default_factory=CircuitConfig)
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def get(self, endpoint: str) -> CircuitBreaker:
        """Return (creating if needed) the breaker for one ``service.method``."""
        breaker = self._breakers.get(endpoint)
        if breaker is None:
            breaker = CircuitBreaker(config=self.default_config, name=endpoint)
            self._breakers[endpoint] = breaker
        return breaker

    def states(self) -> dict[str, BreakerState]:
        """A snapshot of every breaker's state (observability / health)."""
        return {ep: b.state for ep, b in self._breakers.items()}


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitConfig",
]
