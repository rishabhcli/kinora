"""Admission control + backpressure for the QoS fabric (kinora.md §12.2).

This is the *entry* guard: before an item joins the QoS-managed backlog, the
admission policy decides whether to **admit**, **defer** (a soft slow-down signal
to upstream speculation), or **reject** (a hard shed) it. The rules layer the §12.2
guarantees onto the richer QoS model:

* **Committed is always admitted** — the near-reader buffer must never stall.
* **Speculative/cold backpressure** — past the hard ``backpressure_depth`` they're
  rejected (the keyframe ladder covers them, §12.4); between the *slowdown* depth
  and the hard depth they're *deferred* so the Scheduler can throttle speculation
  instead of churning the queue.
* **Per-session cap** — one reader can't hold more than ``session_cap`` in flight.
* **Per-book speculative cap** — one book can't fill the speculative backlog and
  crowd out other books (cross-book fairness lives in :mod:`app.qos.fairness`; this
  is the cheap per-book guard at the door).
* **Expired-deadline reject** — an item the reader already blew past is never
  admitted; its video-seconds would be wasted.

Pure decisions over scalar counters + a clock; a thin :class:`AdmissionPolicy`
bundles a :class:`QoSConfig` so the Scheduler calls one method.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.qos.config import QoSConfig
from app.qos.deadline import is_expired
from app.qos.model import QoSClass, QoSItem, SheddingReason


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    """The outcome of an admission check — admit / defer / reject + reason."""

    admit: bool
    defer: bool
    reason: SheddingReason

    def __bool__(self) -> bool:
        return self.admit


def _admit(reason: SheddingReason = SheddingReason.ADMIT) -> AdmissionVerdict:
    return AdmissionVerdict(admit=True, defer=False, reason=reason)


def _defer(reason: SheddingReason) -> AdmissionVerdict:
    return AdmissionVerdict(admit=False, defer=True, reason=reason)


def _reject(reason: SheddingReason) -> AdmissionVerdict:
    return AdmissionVerdict(admit=False, defer=False, reason=reason)


def decide(
    item: QoSItem,
    now: float,
    *,
    total_depth: int,
    session_inflight: int = 0,
    book_speculative_depth: int = 0,
    config: QoSConfig,
) -> AdmissionVerdict:
    """The pure admission decision for one prospective enqueue.

    ``total_depth`` is the current QoS-managed backlog size; ``session_inflight``
    the session's in-flight/queued count; ``book_speculative_depth`` the book's
    queued speculative count. Committed short-circuits to admit; everything else
    runs the backpressure / cap / deadline guards (cheapest global guard first).
    """
    if item.qos_class is QoSClass.COMMITTED:
        return _admit(SheddingReason.ADMIT)

    # An item the reader already passed is pointless to render.
    if is_expired(item, now, grace_s=config.deadline_expiry_grace_s):
        return _reject(SheddingReason.SHED_OVER_DEADLINE)

    # Hard depth backpressure — the global guard, reject outright.
    if total_depth >= config.backpressure_depth:
        return _reject(SheddingReason.SHED_BACKPRESSURE)

    # Per-session fairness cap.
    if session_inflight >= config.session_cap:
        return _reject(SheddingReason.SHED_TENANT_OVER_FAIR_SHARE)

    # Per-book speculative crowding cap (cheap fairness at the door).
    if (
        item.qos_class is QoSClass.SPECULATIVE
        and book_speculative_depth >= config.per_book_speculative_cap
    ):
        return _reject(SheddingReason.SHED_TENANT_OVER_FAIR_SHARE)

    # Soft slow-down band — admit nothing new but signal upstream to throttle
    # speculation rather than hammer the queue. Cold (keyframe) plan work still
    # defers here too; the reader's near zone is unaffected (committed bypassed).
    if total_depth >= config.speculation_slowdown_depth:
        return _defer(SheddingReason.DEFER_SATURATED)

    return _admit(SheddingReason.ADMIT)


class AdmissionPolicy:
    """A thin stateless wrapper binding a :class:`QoSConfig` to :func:`decide`."""

    __slots__ = ("_config",)

    def __init__(self, config: QoSConfig | None = None) -> None:
        self._config = config or QoSConfig()

    @property
    def config(self) -> QoSConfig:
        return self._config

    def check(
        self,
        item: QoSItem,
        now: float,
        *,
        total_depth: int,
        session_inflight: int = 0,
        book_speculative_depth: int = 0,
    ) -> AdmissionVerdict:
        """Decide admission for ``item`` given the current load snapshot."""
        return decide(
            item,
            now,
            total_depth=total_depth,
            session_inflight=session_inflight,
            book_speculative_depth=book_speculative_depth,
            config=self._config,
        )

    def should_slow_speculation(self, total_depth: int) -> bool:
        """True when upstream should throttle speculative generation (soft signal)."""
        return total_depth >= self._config.speculation_slowdown_depth


__all__ = ["AdmissionPolicy", "AdmissionVerdict", "decide"]
