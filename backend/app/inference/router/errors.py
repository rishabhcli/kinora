"""Typed exception hierarchy for the inference router (``app/inference/router/``).

Every routing decision that can fail surfaces one of these so callers branch on
*why* — admission rejection vs. SLA expiry vs. a misconfiguration — without
parsing strings. The router itself never raises a bare ``Exception``.
"""

from __future__ import annotations


class RouterError(Exception):
    """Base class for every inference-router failure."""


class RouterConfigError(RouterError):
    """A router/component was built with an invalid configuration.

    Raised eagerly at construction time (bad weights, non-positive capacities,
    contradictory tunables) so misconfiguration is a startup failure, never a
    silent runtime drift.
    """


class AdmissionRejected(RouterError):  # noqa: N818 - public name in router contract
    """Admission control refused a request (backpressure / over-capacity).

    Attributes:
        request_id: The rejected request's id.
        reason: A short machine-stable reason code (see
            :class:`~app.inference.router.admission.RejectReason`).
        retry_after_s: Hint, in seconds, for when the client could retry; ``None``
            when a retry is pointless (e.g. the request can never fit).
    """

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        reason: str,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.reason = reason
        self.retry_after_s = retry_after_s


class QueueTimeSLAExpired(RouterError):  # noqa: N818 - public name in router contract
    """A queued request waited past its queue-time SLA before dispatch.

    The router drops such requests at the next scheduling tick rather than
    dispatching work the client has already given up on (§12.2 backpressure).
    """

    def __init__(self, message: str, *, request_id: str, waited_s: float, sla_s: float) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.waited_s = waited_s
        self.sla_s = sla_s


class NoEligibleWorker(RouterError):  # noqa: N818 - public name in router contract
    """No worker can currently accept the request (all full / unhealthy).

    Distinct from :class:`AdmissionRejected`: the request *was* admitted to the
    queue; this is a transient dispatch-time condition the router retries on the
    next tick, not a client-facing rejection.
    """


class BackendError(RouterError):
    """An :class:`~app.inference.router.protocols.InferenceBackend` call failed.

    Wraps the underlying provider/transport error so the router's own typed
    surface stays clean; ``cause`` carries the original exception.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


__all__ = [
    "AdmissionRejected",
    "BackendError",
    "NoEligibleWorker",
    "QueueTimeSLAExpired",
    "RouterConfigError",
    "RouterError",
]
