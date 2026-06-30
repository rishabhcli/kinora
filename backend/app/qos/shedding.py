"""Load shedding — drop the least-valuable droppable work first (kinora.md §12.2/§12.4).

When the backlog is already over a threshold (admission stops *new* work; shedding
trims *existing* work), the fabric drops the least-valuable speculative/cold items
until depth falls to ``shed_target_depth``. The ordering is the heart of the policy:

* **Committed is never shed** — it's the near-reader film; the degradation ladder
  (§12.4) is for *latency*, not for dropping committed jobs.
* **Already-expired deadlines go first** — the reader blew past them; their
  video-seconds are pure waste, so they're the cheapest, most valuable to drop.
* **Then lowest value-density first** — among the rest, the item whose
  ``value / cost_s`` is smallest is shed first, so each drop frees the most
  contended capacity per unit of reader value lost. Ties break toward *cold over
  speculative* (cold is further from the reader) and then oldest-deadline-last
  (keep the work the reader needs soonest).

Pure functions over a list of :class:`~app.qos.model.QoSItem` + a clock. The
selection never mutates input; it returns the victims to drop.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.qos.config import QoSConfig
from app.qos.deadline import is_expired
from app.qos.model import QoSItem, SheddingReason


@dataclass(frozen=True, slots=True)
class ShedVictim:
    """One item selected for load-shedding, with the reason it was chosen."""

    item: QoSItem
    reason: SheddingReason


def _shed_sort_key(item: QoSItem, now: float) -> tuple[int, float, int, float]:
    """Order droppable items worst-first (the front of this order is shed first).

    ``(expired_first, value_density, class_rank_desc, neg_slack)``:

    * **expired_first** — ``0`` for already-expired items so they're dropped before
      anything still useful.
    * **value_density** — ascending: the least worth-per-second goes first.
    * **class_rank_desc** — for equal density, prefer dropping the *colder* class
      (further from the reader): we negate the class int so COLD (2) sorts before
      SPECULATIVE (1).
    * **neg_slack** — final tie-break: drop the item with the *most* slack (least
      urgent) first by sorting on negative slack ascending.
    """
    expired = 0 if is_expired(item, now) else 1
    slack = item.slack_s(now)
    neg_slack = -(slack if slack is not None else float("inf"))
    return (expired, item.value_density(), -int(item.qos_class), neg_slack)


def select_victims(
    items: list[QoSItem],
    now: float,
    *,
    config: QoSConfig,
    target_depth: int | None = None,
) -> list[ShedVictim]:
    """Pick the least-valuable droppable items to shed down to ``target_depth``.

    Returns the victims in the order they should be dropped (worst first). Committed
    items are never candidates. If the backlog is already at/below ``target_depth``
    only *expired* droppable items are shed (always worth reclaiming).
    """
    target = config.shed_target_depth if target_depth is None else target_depth
    droppable = [it for it in items if it.qos_class.droppable]
    if not droppable:
        return []

    ranked = sorted(droppable, key=lambda it: _shed_sort_key(it, now))

    total = len(items)
    overflow = max(0, total - target)
    victims: list[ShedVictim] = []
    for item in ranked:
        if is_expired(item, now, grace_s=config.deadline_expiry_grace_s):
            victims.append(ShedVictim(item, SheddingReason.SHED_OVER_DEADLINE))
            continue
        if len(victims) < overflow:
            victims.append(ShedVictim(item, SheddingReason.SHED_LEAST_VALUE))
        else:
            break
    return victims


def shed(
    items: list[QoSItem],
    now: float,
    *,
    config: QoSConfig,
    target_depth: int | None = None,
) -> tuple[list[QoSItem], list[ShedVictim]]:
    """Apply :func:`select_victims`; return ``(survivors, victims)`` (input unmutated)."""
    victims = select_victims(items, now, config=config, target_depth=target_depth)
    shed_ids = {v.item.id for v in victims}
    survivors = [it for it in items if it.id not in shed_ids]
    return survivors, victims


__all__ = ["ShedVictim", "select_victims", "shed"]
