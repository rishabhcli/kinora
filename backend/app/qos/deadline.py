"""Deadline awareness — EDF-style urgency for near-reader shots (kinora.md §4.3/§4.6).

A shot's *deadline* is the absolute time the reader will reach its span:
``deadline = now + eta_s`` where ``eta = (span_start - w) / v`` (§4.3). Inside a
QoS class, an item the reader needs **soon** must jump ahead of an equally-classed
item the reader needs later — earliest-deadline-first (EDF). But EDF only kicks in
once an item is *urgent* (slack within the horizon); far-off deadlines fall back
to FIFO/age order so a distant-but-already-queued committed shot isn't perturbed
by churn.

Pure functions over :class:`~app.qos.model.QoSItem` + a clock time. No I/O.
"""

from __future__ import annotations

import math

from app.qos.model import QoSItem


def is_urgent(item: QoSItem, now: float, *, horizon_s: float) -> bool:
    """True when the reader needs this item within ``horizon_s`` (or it's already late)."""
    slack = item.slack_s(now)
    if slack is None:
        return False
    return slack <= horizon_s


def is_expired(item: QoSItem, now: float, *, grace_s: float = 0.0) -> bool:
    """True when the deadline has passed by more than ``grace_s`` (reader blew past it)."""
    slack = item.slack_s(now)
    if slack is None:
        return False
    return slack < -abs(grace_s)


def urgency_score(item: QoSItem, now: float, *, horizon_s: float) -> float:
    """A ``[0, 1+]`` urgency: 0 when far from the horizon, 1 at the deadline, >1 late.

    Linear in remaining slack across the horizon. Deadline-less items score 0 so
    they never out-urgent a deadline-bearing peer.
    """
    slack = item.slack_s(now)
    if slack is None:
        return 0.0
    if slack <= 0:
        # Already at/past the deadline — clamp lateness influence so a wildly-late
        # item doesn't dominate ordering forever (it should be shed, not boosted).
        return 1.0 + min(1.0, -slack / max(horizon_s, 1e-9))
    return max(0.0, 1.0 - slack / max(horizon_s, 1e-9))


def edf_key(item: QoSItem, now: float, *, horizon_s: float) -> tuple[int, float, float]:
    """A sort key implementing *bounded* EDF within a class (ascending = serve first).

    Returns ``(urgency_band, deadline_or_inf, enqueued_at)``:

    * **urgency_band** — ``0`` if the item is urgent (within the horizon or late),
      else ``1``. Urgent items always sort before non-urgent ones in the same class.
    * **deadline** — among urgent items, earliest deadline first (true EDF).
    * **enqueued_at** — the FIFO tie-break / the sole order among non-urgent items,
      so far-off work keeps stable arrival order and doesn't thrash.
    """
    urgent_band = 0 if is_urgent(item, now, horizon_s=horizon_s) else 1
    deadline = (
        item.deadline
        if (urgent_band == 0 and item.deadline is not None)
        else math.inf
    )
    return (urgent_band, deadline, item.enqueued_at)


__all__ = ["edf_key", "is_expired", "is_urgent", "urgency_score"]
