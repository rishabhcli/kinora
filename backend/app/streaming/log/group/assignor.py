"""Partition assignment strategies for consumer groups.

When a group's membership or subscription changes, the coordinator runs an
*assignor* to map subscribed partitions onto members. These are pure functions of
``(members → subscribed topics, topic → partition count)`` so they're trivially
testable and deterministic (members and partitions are processed in sorted
order, so the same inputs always yield the same assignment).

Three Kafka-equivalent strategies:

* :class:`RangeAssignor` — per *topic*, slice that topic's partitions into
  contiguous ranges across the members subscribed to it. Co-locates the same
  partition index of co-subscribed topics on one member (good for joins).
* :class:`RoundRobinAssignor` — lay all ``(topic, partition)`` pairs end to end
  and deal them round-robin across members. Best balance across many topics.
* :class:`CooperativeStickyAssignor` — balance like round-robin but *minimise
  movement*: keep a member's previously-owned partitions where possible, only
  reassigning the surplus. Enables incremental (cooperative) rebalancing — a
  member revokes only what it loses, not its whole assignment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.streaming.log.errors import InvalidConfigError
from app.streaming.log.record import TopicPartition

__all__ = [
    "Assignor",
    "CooperativeStickyAssignor",
    "RangeAssignor",
    "RoundRobinAssignor",
    "get_assignor",
]


@runtime_checkable
class Assignor(Protocol):
    """Maps group members to partition assignments."""

    name: str

    def assign(
        self,
        members: Mapping[str, tuple[str, ...]],
        partitions_per_topic: Mapping[str, int],
        current: Mapping[str, tuple[TopicPartition, ...]] | None = None,
    ) -> dict[str, tuple[TopicPartition, ...]]:
        """Return ``member_id → assigned partitions``.

        ``members`` maps member id → its subscribed topic tuple. ``current`` is
        the previous assignment (used by sticky strategies; ignored otherwise).
        """
        ...


def _subscribed_members(members: Mapping[str, tuple[str, ...]], topic: str) -> list[str]:
    """Sorted member ids subscribed to ``topic``."""
    return sorted(m for m, subs in members.items() if topic in subs)


def _all_subscribed_topics(members: Mapping[str, tuple[str, ...]]) -> list[str]:
    """Sorted union of every member's subscriptions."""
    topics: set[str] = set()
    for subs in members.values():
        topics.update(subs)
    return sorted(topics)


class RangeAssignor:
    """Contiguous per-topic range assignment (Kafka's default ``range``)."""

    name = "range"

    def assign(
        self,
        members: Mapping[str, tuple[str, ...]],
        partitions_per_topic: Mapping[str, int],
        current: Mapping[str, tuple[TopicPartition, ...]] | None = None,
    ) -> dict[str, tuple[TopicPartition, ...]]:
        out: dict[str, list[TopicPartition]] = {m: [] for m in members}
        for topic in _all_subscribed_topics(members):
            subscribers = _subscribed_members(members, topic)
            if not subscribers:
                continue
            n = partitions_per_topic.get(topic, 0)
            per_member, remainder = divmod(n, len(subscribers))
            start = 0
            for idx, member in enumerate(subscribers):
                count = per_member + (1 if idx < remainder else 0)
                for p in range(start, start + count):
                    out[member].append(TopicPartition(topic, p))
                start += count
        return {m: tuple(parts) for m, parts in out.items()}


class RoundRobinAssignor:
    """Round-robin every ``(topic, partition)`` across members (Kafka ``roundrobin``)."""

    name = "roundrobin"

    def assign(
        self,
        members: Mapping[str, tuple[str, ...]],
        partitions_per_topic: Mapping[str, int],
        current: Mapping[str, tuple[TopicPartition, ...]] | None = None,
    ) -> dict[str, tuple[TopicPartition, ...]]:
        out: dict[str, list[TopicPartition]] = {m: [] for m in members}
        all_tps = self._ordered_partitions(members, partitions_per_topic)
        member_ids = sorted(members)
        if not member_ids:
            return {}
        cursor = 0
        for tp in all_tps:
            # Advance to the next member subscribed to this partition's topic.
            for _ in range(len(member_ids)):
                member = member_ids[cursor % len(member_ids)]
                cursor += 1
                if tp.topic in members[member]:
                    out[member].append(tp)
                    break
        return {m: tuple(parts) for m, parts in out.items()}

    @staticmethod
    def _ordered_partitions(
        members: Mapping[str, tuple[str, ...]], partitions_per_topic: Mapping[str, int]
    ) -> list[TopicPartition]:
        tps: list[TopicPartition] = []
        for topic in _all_subscribed_topics(members):
            for p in range(partitions_per_topic.get(topic, 0)):
                tps.append(TopicPartition(topic, p))
        return tps


class CooperativeStickyAssignor:
    """Balanced assignment that preserves prior ownership to minimise churn.

    Phase 1: keep each member's still-valid current partitions, up to a fair
    ceiling (``ceil(total / members)``). Phase 2: deal the unassigned remainder
    to the least-loaded members. Output is deterministic (sorted tie-breaks),
    and a member never holds a partition for a topic it no longer subscribes to.
    """

    name = "cooperative-sticky"

    def assign(
        self,
        members: Mapping[str, tuple[str, ...]],
        partitions_per_topic: Mapping[str, int],
        current: Mapping[str, tuple[TopicPartition, ...]] | None = None,
    ) -> dict[str, tuple[TopicPartition, ...]]:
        member_ids = sorted(members)
        if not member_ids:
            return {}
        current = current or {}

        all_tps = RoundRobinAssignor._ordered_partitions(members, partitions_per_topic)
        all_set = set(all_tps)
        total = len(all_tps)
        max_per_member = -(-total // len(member_ids))  # ceil

        out: dict[str, list[TopicPartition]] = {m: [] for m in member_ids}
        claimed: set[TopicPartition] = set()

        # Phase 1 — retain valid current ownership, capped at the fair ceiling.
        for member in member_ids:
            for tp in sorted(current.get(member, ())):
                if tp not in all_set or tp in claimed:
                    continue
                if tp.topic not in members[member]:
                    continue
                if len(out[member]) >= max_per_member:
                    continue
                out[member].append(tp)
                claimed.add(tp)

        # Phase 2 — deal the remainder to the least-loaded eligible member.
        remaining = [tp for tp in all_tps if tp not in claimed]
        for tp in remaining:
            eligible = [m for m in member_ids if tp.topic in members[m]]
            if not eligible:
                continue
            target = min(eligible, key=lambda m: (len(out[m]), m))
            out[target].append(tp)
            claimed.add(tp)

        return {m: tuple(sorted(parts)) for m, parts in out.items()}


_REGISTRY: dict[str, type[Assignor]] = {
    RangeAssignor.name: RangeAssignor,
    RoundRobinAssignor.name: RoundRobinAssignor,
    CooperativeStickyAssignor.name: CooperativeStickyAssignor,
}


def get_assignor(name: str) -> Assignor:
    """Look up an assignor by Kafka-style protocol name; raise on unknown."""
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise InvalidConfigError(
            f"unknown assignment protocol {name!r}; known: {sorted(_REGISTRY)}"
        ) from None
