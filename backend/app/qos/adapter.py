"""Map the live render queue onto the QoS model without touching it (additive seam).

The QoS fabric is pure policy over :class:`~app.qos.model.QoSItem`; the production
queue speaks :class:`app.queue.redis_queue.QueuedJob` / ``RenderPriority``. This
adapter is the *only* place the two meet, so the policy stays infra-free and the
existing queue is never rewritten. A caller (the Scheduler, or a future QoS-aware
dispatcher) can lift queued jobs into QoS items, run a policy decision, and act on
the result against the real queue.

All functions are pure conversions — no Redis, no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from app.qos.model import QoSClass, QoSItem

if TYPE_CHECKING:
    from app.db.models.enums import RenderPriority


class _JobLike(Protocol):
    """The slice of ``QueuedJob`` the adapter reads (duck-typed for testability)."""

    id: str
    priority: RenderPriority
    book_id: str
    session_id: str | None
    target_word: int
    target_duration_s: float
    reserved_video_s: float


def job_to_qos_item(
    job: _JobLike,
    *,
    now: float,
    focus_word: int | None = None,
    velocity_wps: float = 4.0,
    enqueued_at: float | None = None,
    tenant_id: str | None = None,
    value: float | None = None,
) -> QoSItem:
    """Lift a queued render job into a :class:`QoSItem` for policy evaluation.

    The deadline is derived from the §4.3 reading-time ETA when a ``focus_word`` is
    given: ``deadline = now + (target_word - focus_word) / v``. Without a focus word
    the item is deadline-less (treated as plan/cold for EDF). ``value`` defaults to a
    class-tiered baseline scaled by the reserved video-seconds (more expensive shots
    that the reader needs are worth more to keep).
    """
    qos_class = QoSClass.from_priority(job.priority)
    deadline: float | None = None
    eta_s: float | None = None
    if focus_word is not None:
        v = max(abs(velocity_wps), 0.1)
        eta_s = (job.target_word - focus_word) / v
        deadline = now + eta_s
    cost_s = max(float(job.reserved_video_s) or float(job.target_duration_s), 1e-9)
    if value is None:
        base = {QoSClass.COMMITTED: 10.0, QoSClass.SPECULATIVE: 4.0, QoSClass.COLD: 1.0}[qos_class]
        value = base
    return QoSItem(
        id=job.id,
        qos_class=qos_class,
        book_id=job.book_id,
        session_id=job.session_id,
        tenant_id=tenant_id,
        enqueued_at=now if enqueued_at is None else enqueued_at,
        deadline=deadline,
        eta_s=eta_s,
        value=value,
        cost_s=cost_s,
    )


def jobs_to_qos_items(jobs: list[Any], **kwargs: Any) -> list[QoSItem]:
    """Vectorised :func:`job_to_qos_item` over a list of jobs (same kwargs)."""
    return [job_to_qos_item(job, **kwargs) for job in jobs]


__all__ = ["job_to_qos_item", "jobs_to_qos_items"]
