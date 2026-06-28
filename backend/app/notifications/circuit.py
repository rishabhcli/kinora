"""Per-target circuit breaker for outbound delivery (kinora.md §12).

A webhook endpoint (or a flaky email/push provider) that is failing should be
*fenced off* rather than hammered on every retry — otherwise a dead endpoint
turns into a queue-clogging retry storm. The classic three-state breaker:

* **closed** — calls flow; consecutive failures are counted. At ``failure_threshold``
  the breaker trips **open**.
* **open** — calls are rejected immediately (``CircuitOpenError``) until
  ``reset_timeout_s`` elapses, then it moves to **half-open**.
* **half-open** — a single trial call is allowed. Success closes the breaker (and
  resets the counter); failure re-opens it for another cooldown.

The breaker is a pure state machine driven by an injected ``clock`` so its timing
is deterministic in tests. It holds no I/O; the caller wraps each attempt with
:meth:`allow` (guard) + :meth:`record_success` / :meth:`record_failure`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    """The three breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """A single-target circuit breaker.

    Construct one per delivery target (webhook endpoint id, or a channel name for
    shared providers). Thread-affinity is the caller's concern — the breaker is a
    plain object, not async; the dispatcher serializes access per target.
    """

    failure_threshold: int = 5
    reset_timeout_s: float = 30.0
    #: How many half-open trial successes are needed to fully close.
    half_open_max_calls: int = 1
    clock: Callable[[], float] = field(default=time.monotonic)

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _half_open_successes: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        """The current state, transitioning open→half-open lazily on read/allow."""
        self._maybe_half_open()
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow(self) -> bool:
        """Whether a call may proceed right now (transitions open→half-open on timeout)."""
        self._maybe_half_open()
        return self._state is not CircuitState.OPEN

    def retry_after_s(self) -> float | None:
        """Seconds until an open breaker will admit a trial call (``None`` if not open)."""
        if self._state is not CircuitState.OPEN:
            return None
        remaining = self.reset_timeout_s - (self.clock() - self._opened_at)
        return max(remaining, 0.0)

    def record_success(self) -> None:
        """A successful call: close the breaker and reset the failure counter."""
        self._consecutive_failures = 0
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self.half_open_max_calls:
                self._close()
        else:
            self._close()

    def record_failure(self) -> None:
        """A failed call: trip open at threshold, or re-open from half-open at once."""
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN:
            self._open()
            return
        if self._consecutive_failures >= self.failure_threshold:
            self._open()

    # -- internal transitions ------------------------------------------------ #

    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self.clock() - self._opened_at >= self.reset_timeout_s
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_successes = 0

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self.clock()
        self._half_open_successes = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._half_open_successes = 0


class CircuitRegistry:
    """A lazy registry of per-target :class:`CircuitBreaker` instances.

    The dispatcher looks up a breaker by target key; one is created with the
    shared policy on first use. Kept tiny and in-process — durable breaker state
    is not required (a fresh process simply starts closed and re-learns).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, target: str) -> CircuitBreaker:
        """Return (creating if needed) the breaker for ``target``."""
        breaker = self._breakers.get(target)
        if breaker is None:
            breaker = CircuitBreaker(
                failure_threshold=self._failure_threshold,
                reset_timeout_s=self._reset_timeout_s,
                clock=self._clock,
            )
            self._breakers[target] = breaker
        return breaker

    def snapshot(self) -> dict[str, CircuitState]:
        """A map of target → current state (for observability / the metrics panel)."""
        return {target: breaker.state for target, breaker in self._breakers.items()}


__all__ = ["CircuitBreaker", "CircuitRegistry", "CircuitState"]
