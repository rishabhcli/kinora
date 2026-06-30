"""A 429/Retry-After-aware submission throttle that paces under each provider's wall.

The round-1 router fails over on a 429; the resilience layer has an AIMD adaptive
bucket for *retries*. This is the **submission pacer** that sits in front of new
work: it spaces submissions so we approach a provider's requests-per-minute ceiling
without overrunning it, and — crucially — it honours a provider's ``Retry-After``
by parking *all* submissions to that provider until the server-suggested instant
passes, then easing back in.

Two cleanly separated mechanisms:

* **Steady-state pacing (a token bucket).** Tokens refill continuously at
  ``rate_per_min`` up to a ``burst`` ceiling; a submission spends one token. A full
  bucket lets ``burst`` submissions go back-to-back, after which submissions pace at
  the refill rate. This is the same shape as the resilience-layer bucket, kept pure
  and clock-driven (no continuous task, just arithmetic on the injected clock).
* **Retry-After park.** :meth:`note_rate_limited` records a hard ``not before``
  instant from the server's ``Retry-After`` (or a multiplicative fallback that grows
  with consecutive 429s, capped). While the park is in the future, :meth:`acquire_delay`
  returns the remaining wait and admits nothing, regardless of token balance.

A clean success after a backoff is the recovery signal (:meth:`note_success`).
Everything is computed against the injected clock and returns *how long to wait*
rather than sleeping, so the caller (or a fake clock in tests) controls time; the
async :meth:`throttle` convenience sleeps that long via an injected sleep.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .clock import Clock, monotonic

#: Injectable async sleep (tests pass one that advances a fake clock).
AsyncSleep = Callable[[float], Awaitable[None]]

#: Floating-point slack so a refill that lands a hair below one token still counts.
_TOKEN_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class ThrottleConfig:
    """Tunables for :class:`ProviderThrottle` (no env reads)."""

    #: Target steady-state submission rate (requests per minute). 0 ⇒ unpaced.
    rate_per_min: float = 60.0
    #: Bucket capacity — how many submissions may go out back-to-back when full.
    burst: int = 4
    #: Multiplicative backoff base when no Retry-After header is present (seconds).
    fallback_backoff_s: float = 2.0
    #: Backoff grows ``fallback_backoff_s * multiplier**(consecutive-1)``…
    backoff_multiplier: float = 2.0
    #: …capped here so a runaway provider can't park us forever.
    max_backoff_s: float = 120.0

    def __post_init__(self) -> None:
        if self.rate_per_min < 0:
            raise ValueError("rate_per_min must be >= 0")
        if self.burst < 1:
            raise ValueError("burst must be >= 1")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1")

    @property
    def refill_per_s(self) -> float:
        """Token refill rate (tokens/second) implied by ``rate_per_min``."""
        return self.rate_per_min / 60.0


@dataclass(frozen=True, slots=True)
class ThrottleState:
    """An inspectable snapshot of a throttle's pacing/backoff state."""

    provider: str
    not_before: float
    consecutive_throttles: int
    burst_tokens: float
    backed_off: bool
    now: float

    @property
    def wait_s(self) -> float:
        """How long until the next submission may go out (0 if ready).

        The wait is the *max* of the Retry-After park remainder and the time to
        accrue one pacing token — whichever gates longer.
        """
        return max(0.0, self.not_before - self.now)


class ProviderThrottle:
    """Pace submissions for one provider, parking on observed 429/Retry-After.

    A submission flow is::

        wait = throttle.acquire_delay()   # >0 ⇒ caller should wait this long
        # ... submit ...
        throttle.note_success()           # on 2xx
        throttle.note_rate_limited(retry_after_s=...)   # on 429

    :meth:`acquire_delay` **reserves** a pacing token when it returns 0 (so a tight
    loop genuinely spaces out); a positive return reserves nothing.
    """

    def __init__(
        self,
        provider: str,
        config: ThrottleConfig | None = None,
        *,
        clock: Clock = monotonic,
        sleep: AsyncSleep | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or ThrottleConfig()
        self._clock = clock
        self._sleep = sleep
        self._not_before = 0.0
        self._consecutive = 0
        self._backed_off = False
        # Token bucket: starts full so a fresh provider gets its burst.
        self._tokens = float(self.config.burst)
        self._updated = clock()

    # -- token bucket ----------------------------------------------------- #

    def _refill(self, now: float) -> None:
        refill = self.config.refill_per_s
        if refill <= 0:
            self._tokens = float(self.config.burst)
            self._updated = now
            return
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(float(self.config.burst), self._tokens + elapsed * refill)
        self._updated = now

    # -- introspection ---------------------------------------------------- #

    def state(self) -> ThrottleState:
        now = self._clock()
        self._refill(now)
        return ThrottleState(
            provider=self.provider,
            not_before=self._effective_not_before(now),
            consecutive_throttles=self._consecutive,
            burst_tokens=self._tokens,
            backed_off=self._backed_off,
            now=now,
        )

    def _effective_not_before(self, now: float) -> float:
        """The earliest instant a submission may go out (park OR token accrual)."""
        park_wait = max(0.0, self._not_before - now)
        token_wait = 0.0
        if self._tokens + _TOKEN_EPSILON < 1.0:
            refill = self.config.refill_per_s
            if refill > 0:
                token_wait = (1.0 - self._tokens) / refill
        return now + max(park_wait, token_wait)

    def is_backed_off(self) -> bool:
        """True while parked behind a Retry-After/backoff that hasn't elapsed."""
        return self._backed_off and self._clock() < self._not_before

    # -- pacing ----------------------------------------------------------- #

    def acquire_delay(self) -> float:
        """Return how long to wait before submitting; reserve a token if ready.

        Returns ``0.0`` and spends one pacing token when a submission may go out
        now; otherwise returns the positive wait until the next opening — the larger
        of the Retry-After park remainder and the time to accrue one token. When a
        backoff window has elapsed, the backed-off flag clears so the *next* success
        is recognised as a recovery.
        """
        now = self._clock()
        self._refill(now)

        park_wait = self._not_before - now
        if park_wait <= 0 and self._backed_off:
            # The park has elapsed; clear the flag (recovery will confirm next 2xx).
            self._backed_off = False
            park_wait = 0.0

        if park_wait > 0:
            return park_wait

        if self._tokens + _TOKEN_EPSILON >= 1.0:
            self._tokens = max(0.0, self._tokens - 1.0)
            return 0.0

        refill = self.config.refill_per_s
        if refill <= 0:
            return 0.0
        return (1.0 - self._tokens) / refill

    async def throttle(self) -> float:
        """Await the required pacing delay (needs an injected ``sleep``); return it.

        Loops so that after sleeping out a wait, it re-acquires (a single sleep may
        not have accrued a full token if the clock advanced exactly the wait).
        """
        if self._sleep is None:
            raise RuntimeError("ProviderThrottle.throttle requires an injected sleep")
        total = 0.0
        while True:
            delay = self.acquire_delay()
            if delay <= 0:
                return total
            total += delay
            await self._sleep(delay)

    # -- feedback --------------------------------------------------------- #

    def note_success(self) -> bool:
        """Record a clean submission; returns True if this cleared a backoff.

        Clearing a backoff is the ``THROTTLE_RECOVERED`` signal the governor emits.
        """
        recovered = self._consecutive > 0
        self._consecutive = 0
        self._backed_off = False
        return recovered

    def note_rate_limited(self, *, retry_after_s: float | None = None) -> float:
        """Park submissions after a 429; return the backoff applied (seconds).

        Honours a server ``Retry-After`` exactly when present (capped at
        ``max_backoff_s``); otherwise applies a multiplicative fallback that grows
        with consecutive throttles. The park instant is the *max* of the existing
        ``not_before`` and ``now + backoff`` so overlapping 429s never shorten the
        wait. Tokens are drained so we don't immediately spend a burst slot on exit.
        """
        self._consecutive += 1
        if retry_after_s is not None and retry_after_s > 0:
            backoff = min(retry_after_s, self.config.max_backoff_s)
        else:
            grow = self.config.backoff_multiplier ** (self._consecutive - 1)
            backoff = min(self.config.fallback_backoff_s * grow, self.config.max_backoff_s)
        now = self._clock()
        self._not_before = max(self._not_before, now + backoff)
        self._backed_off = True
        self._tokens = 0.0
        self._updated = now
        return backoff


__all__ = [
    "AsyncSleep",
    "ProviderThrottle",
    "ThrottleConfig",
    "ThrottleState",
]
