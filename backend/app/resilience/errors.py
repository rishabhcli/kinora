"""The cross-cutting resilience error taxonomy.

Every external call in Kinora — a DashScope render, a Redis ``BRPOPLPUSH``, a
Postgres write — can fail in one of a small number of *shapes*, and the right
reaction depends on the shape, not the subsystem. These exceptions name those
shapes once so the retry / breaker / bulkhead policies (and callers) branch on
*why* a call failed without string-matching.

This taxonomy is intentionally **independent** of :mod:`app.providers.errors`
(which is DashScope-specific): the resilience framework wraps Redis and Postgres
too. :func:`classify_exception` maps the well-known provider / stdlib exceptions
onto :attr:`ResilienceError.retryable`, and :class:`RetryPolicy` accepts any
predicate, so adopting code never has to translate by hand.

Distinction that matters:

* ``retryable`` — could the *same* call plausibly succeed on a retry? (timeouts,
  5xx, connection resets, throttles → yes; bad request, auth → no).
* The policy-raised wrappers (:class:`RetriesExhausted`, :class:`DeadlineExceeded`,
  :class:`CircuitOpen`, :class:`BulkheadFull`, :class:`CallTimeout`,
  :class:`RateLimitExceeded`) describe a *resilience decision*, not a transport
  failure — they always carry the underlying cause when one exists.
"""

from __future__ import annotations


class ResilienceError(Exception):
    """Base for everything the resilience layer raises or classifies.

    ``retryable`` is the single most load-bearing attribute: the default retry
    predicate keys off it. ``cause`` carries the original exception when this
    wraps one (the policy decisions below always set it).
    """

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        if self.cause is not None and str(self.cause) and str(self.cause) != self.message:
            return f"{self.message} (cause: {type(self.cause).__name__}: {self.cause})"
        return self.message


# --------------------------------------------------------------------------- #
# Transport-shaped failures (what an external call can do to you)
# --------------------------------------------------------------------------- #


class TransientError(ResilienceError):
    """A transient failure (network blip, 5xx, dropped connection). Safe to retry."""

    retryable = True


class TimeoutError_(TransientError):  # noqa: N801, N818 - trailing _ avoids shadowing builtin TimeoutError
    """The call exceeded its per-attempt timeout (transport-level). Retryable."""


class RateLimitedError(TransientError):
    """A throttle signal (HTTP 429 / quota). Retryable after a backoff.

    ``retry_after_s`` carries any server-supplied ``Retry-After`` so the backoff
    schedule can honor the server's own hint.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.retry_after_s = retry_after_s


class PermanentError(ResilienceError):
    """A failure retrying cannot fix (bad request, validation). Not retryable."""

    retryable = False


class AuthError(PermanentError):
    """Invalid / missing credentials or insufficient permission. Not retryable."""


# --------------------------------------------------------------------------- #
# Resilience *decisions* (raised by the policies themselves)
# --------------------------------------------------------------------------- #


class RetriesExhausted(ResilienceError):  # noqa: N818 - public taxonomy name (not an "...Error")
    """Every retry attempt failed. Carries the last underlying error as ``cause``.

    ``attempts`` is how many tries were made before giving up.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.attempts = attempts


class DeadlineExceeded(ResilienceError):  # noqa: N818 - public taxonomy name (not an "...Error")
    """The overall retry *budget* (wall deadline across attempts) ran out.

    Distinct from :class:`CallTimeout` (one attempt) and :class:`RetriesExhausted`
    (attempt count): this is the total-time guardrail. Carries the last cause.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        elapsed_s: float,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.attempts = attempts
        self.elapsed_s = elapsed_s


class CircuitOpen(ResilienceError):  # noqa: N818 - public taxonomy name (not an "...Error")
    """The circuit breaker is open; the call was rejected without being attempted.

    Retryable in the sense that a *later* call may be admitted once the breaker
    half-opens — so callers backing off and retrying later is correct.
    """

    retryable = True

    def __init__(self, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class BulkheadFull(ResilienceError):  # noqa: N818 - public taxonomy name (not an "...Error")
    """The dependency's concurrency bulkhead is saturated and the wait queue is
    full (or the acquire timed out). Shedding load deliberately — retryable later.
    """

    retryable = True

    def __init__(self, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class CallTimeout(TimeoutError_):
    """The :func:`call_with_timeout` wrapper cancelled a single attempt.

    A subclass of :class:`TimeoutError_` so the default predicate treats it as a
    retryable per-attempt timeout.
    """


class RateLimitExceeded(ResilienceError):  # noqa: N818 - public taxonomy name (not an "...Error")
    """A client-side rate limiter refused (non-blocking acquire / acquire timeout).

    Distinct from :class:`RateLimitedError` (that is the *server* throttling us);
    this is *our* limiter shedding to protect a downstream. Retryable later.
    """

    retryable = True

    def __init__(self, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class ChaosInjectedError(TransientError):
    """A fault deliberately injected by the chaos harness (tests / soak only).

    Subclasses :class:`TransientError` so retry policies treat an injected fault
    exactly like a real transient one — that is the whole point of the harness.
    """


def classify_exception(exc: BaseException) -> bool:
    """Best-effort: is ``exc`` retryable? The default retry predicate's fallback.

    Order matters — most specific first:

    * a :class:`ResilienceError` already knows (``exc.retryable``);
    * a provider :class:`app.providers.errors.ProviderError` exposes ``retryable``;
    * stdlib / asyncio :class:`TimeoutError` and :class:`ConnectionError` are
      transient;
    * everything else is treated as **not** retryable (fail fast — a surprise
      ``ValueError`` is a bug, not a blip).

    Imports of the provider taxonomy are local + guarded so this module stays
    importable with nothing but the stdlib.
    """
    if isinstance(exc, ResilienceError):
        return exc.retryable
    # Provider taxonomy (optional dependency direction: resilience does not need it).
    try:
        from app.providers.errors import ProviderError
    except Exception:  # pragma: no cover - provider package always present in app
        pass
    else:
        if isinstance(exc, ProviderError):
            return bool(getattr(exc, "retryable", False))
    return isinstance(exc, TimeoutError | ConnectionError)


__all__ = [
    "AuthError",
    "BulkheadFull",
    "CallTimeout",
    "ChaosInjectedError",
    "CircuitOpen",
    "DeadlineExceeded",
    "PermanentError",
    "RateLimitExceeded",
    "RateLimitedError",
    "ResilienceError",
    "RetriesExhausted",
    "TimeoutError_",
    "TransientError",
    "classify_exception",
]
