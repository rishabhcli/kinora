"""Throttle-fabric exceptions.

The hierarchy is deliberately shallow and typed so callers can distinguish the
three outcomes that matter operationally:

* :class:`Throttled` — the request was *denied by policy* (some limit in the
  hierarchy is exhausted). It carries a precise ``retry_after`` so the caller —
  or an HTTP layer — can wait exactly long enough and try again. This is the
  normal, expected back-pressure signal, **not** an error in the bug sense.

* :class:`LeaseUnavailable` — a distributed concurrency lease could not be taken
  because the fleet is already at its in-flight cap. Like :class:`Throttled` it
  carries a hint for when to retry, but it is a *concurrency* limit, not a rate
  limit, so it is its own type.

* :class:`StoreUnavailable` — the backing store (redis) is unreachable or a unit
  script failed. The client's *fail-open / fail-closed* policy decides whether
  this propagates or is swallowed (allow-through); see
  :mod:`app.throttle.client`.

All three subclass :class:`ThrottleError` so a caller that just wants "did the
fabric stop me" can catch the base.
"""

from __future__ import annotations


class ThrottleError(Exception):
    """Base for everything the throttle fabric raises."""


class Throttled(ThrottleError):
    """A rate limit denied the request.

    :param retry_after: Seconds to wait before the request would be admitted, as
        computed by the limiter (never negative). For a hierarchy this is the
        *maximum* wait across the binding limits — wait that long and *every*
        limit will admit. ``0.0`` means "retry immediately" (rare; usually a
        race that just cleared).
    :param scope: The dotted scope key of the binding (most-restrictive) limit,
        for diagnostics and metrics labelling (e.g. ``"provider:dashscope"``).
    :param limit: Human label of the limit kind that bound (e.g.
        ``"token_bucket"``), for diagnostics.
    """

    def __init__(
        self,
        retry_after: float,
        *,
        scope: str = "",
        limit: str = "",
    ) -> None:
        self.retry_after = max(0.0, float(retry_after))
        self.scope = scope
        self.limit = limit
        super().__init__(
            f"throttled scope={scope!r} limit={limit!r} retry_after={self.retry_after:.3f}s"
        )


class LeaseUnavailable(ThrottleError):
    """No distributed concurrency lease was free (fleet at its in-flight cap).

    :param retry_after: A *hint* (seconds) for when a slot might free up. Unlike a
        rate limit this cannot be computed exactly — it depends on when some other
        holder releases — so it is the configured poll interval, not a guarantee.
    :param scope: The concurrency-pool scope key, for diagnostics.
    :param in_flight: The observed in-flight count at denial time.
    :param capacity: The pool capacity.
    """

    def __init__(
        self,
        retry_after: float,
        *,
        scope: str = "",
        in_flight: int = 0,
        capacity: int = 0,
    ) -> None:
        self.retry_after = max(0.0, float(retry_after))
        self.scope = scope
        self.in_flight = in_flight
        self.capacity = capacity
        super().__init__(
            f"lease unavailable scope={scope!r} in_flight={in_flight}/{capacity} "
            f"retry_after={self.retry_after:.3f}s"
        )


class StoreUnavailable(ThrottleError):
    """The backing store failed (unreachable, timeout, or script error).

    Whether this is fatal is the client's decision: under *fail-open* the client
    catches it and admits the request (availability over correctness); under
    *fail-closed* it denies. The original cause is chained for logs.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


__all__ = [
    "LeaseUnavailable",
    "StoreUnavailable",
    "Throttled",
    "ThrottleError",
]
