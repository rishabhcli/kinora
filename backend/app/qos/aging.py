"""Aging / anti-starvation — promote long-waiting low-class work (kinora.md §4.9/§12.2).

Strict priority alone starves the cold lane: while committed work keeps arriving,
a cold item could wait forever. The WFQ tier (:mod:`app.qos.wfq`) gives cold a
*share*, and aging is the second guard — a queued speculative/cold item earns a
priority **boost step** for every ``aging_step_s`` it waits, up to ``aging_max_boost``
steps, so an old cold job eventually competes with (and is served alongside)
speculative work. Committed never ages (it's already top) and aging is bounded so a
flood of ancient cold work can't invert the near-reader guarantee.

The *effective class* an item is scheduled at is ``max(0, qos_class - boost)`` — a
lower number is higher priority. Pure functions over an item + clock time.
"""

from __future__ import annotations

from app.qos.config import QoSConfig
from app.qos.model import QoSClass, QoSItem


def aging_boost(item: QoSItem, now: float, *, config: QoSConfig) -> int:
    """How many priority steps ``item`` has earned by waiting (bounded).

    Committed work never ages (returns 0). Otherwise one step per ``aging_step_s``
    waited, clamped to ``aging_max_boost``.
    """
    if item.qos_class is QoSClass.COMMITTED:
        return 0
    waited = item.wait_s(now)
    steps = int(waited // config.aging_step_s)
    return min(steps, config.aging_max_boost)


def effective_class_value(item: QoSItem, now: float, *, config: QoSConfig) -> int:
    """The aged effective priority value (lower = higher); never below COMMITTED."""
    boost = aging_boost(item, now, config=config)
    return max(int(QoSClass.COMMITTED), int(item.qos_class) - boost)


def apply_aging(items: list[QoSItem], now: float, *, config: QoSConfig) -> None:
    """Stamp each item's ``aging_boost`` in place from its current wait (idempotent)."""
    for item in items:
        item.aging_boost = aging_boost(item, now, config=config)


def is_starving(
    item: QoSItem, now: float, *, config: QoSConfig, starvation_factor: float = 2.0
) -> bool:
    """True when a non-committed item has waited long enough to be at max boost.

    A diagnostic the fabric surfaces so operators can see anti-starvation firing:
    an item that has waited ``aging_max_boost * aging_step_s * starvation_factor``
    has been promoted as far as aging allows and is still queued.
    """
    if item.qos_class is QoSClass.COMMITTED or config.aging_max_boost == 0:
        return False
    threshold = config.aging_max_boost * config.aging_step_s * starvation_factor
    return item.wait_s(now) >= threshold


__all__ = ["aging_boost", "apply_aging", "effective_class_value", "is_starving"]
