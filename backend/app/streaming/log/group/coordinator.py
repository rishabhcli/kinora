"""The consumer-group coordinator — membership, generations, rebalance, offsets.

A :class:`GroupCoordinator` is an in-process state machine that backs the
``Broker`` group methods. It owns, per group:

* **membership** — the live members and each member's subscription + last
  heartbeat time (an injectable clock makes session-timeout eviction
  deterministic in tests);
* **generation** — a monotonic epoch bumped on every rebalance. A member's
  assignment + commits are valid only at the current generation; a stale member
  is told to rejoin (``RebalanceInProgressError`` / ``CommitConflictError``);
* **assignment** — recomputed by the configured :class:`~app.streaming.log.group.
  assignor.Assignor` whenever membership/subscription changes, fed the previous
  assignment so cooperative-sticky can minimise churn;
* **committed offsets** — the group's durable read position per partition.

This is the heart of consumer *groups*: rebalancing partitions across members,
fencing stale generations, and persisting progress. The broker delegates the
``Broker`` group + offset-store methods straight to one of these per group.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.streaming.log.broker import GroupDescription, JoinResult, MemberInfo
from app.streaming.log.errors import (
    CommitConflictError,
    UnknownMemberError,
)
from app.streaming.log.group.assignor import Assignor, get_assignor
from app.streaming.log.record import TopicPartition

__all__ = ["GroupCoordinator", "GroupMember", "GroupState"]


def _uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


@dataclass(slots=True)
class GroupMember:
    """A live member of a consumer group."""

    member_id: str
    subscription: tuple[str, ...]
    assignment: tuple[TopicPartition, ...] = ()
    last_heartbeat: float = 0.0


@dataclass(slots=True)
class GroupState:
    """All coordinator state for a single consumer group."""

    group_id: str
    generation: int = 0
    members: dict[str, GroupMember] = field(default_factory=dict)
    committed: dict[TopicPartition, int] = field(default_factory=dict)
    leader: str | None = None


class GroupCoordinator:
    """Coordinates one or many consumer groups (membership + offsets + rebalance).

    ``partition_counts`` is a callback into the broker so the coordinator always
    assigns against the *current* topic partition counts. ``clock`` supplies the
    monotonic time used for heartbeat/session-timeout eviction.
    """

    def __init__(
        self,
        *,
        partition_counts: Callable[[str], int],
        clock: Callable[[], float] | None = None,
        session_timeout_s: float = 30.0,
        default_protocol: str = "range",
    ) -> None:
        self._partition_counts = partition_counts
        self._clock = clock or _default_clock()
        self._session_timeout_s = session_timeout_s
        self._default_protocol = default_protocol
        self._groups: dict[str, GroupState] = {}
        self._protocols: dict[str, str] = {}

    # --- helpers --------------------------------------------------------- #

    def _group(self, group_id: str) -> GroupState:
        state = self._groups.get(group_id)
        if state is None:
            state = GroupState(group_id=group_id)
            self._groups[group_id] = state
        return state

    def _assignor(self, group_id: str) -> Assignor:
        return get_assignor(self._protocols.get(group_id, self._default_protocol))

    # --- membership / rebalance ----------------------------------------- #

    def join(
        self,
        group_id: str,
        *,
        member_id: str | None,
        subscription: tuple[str, ...],
        protocol: str | None = None,
    ) -> JoinResult:
        """Join/rejoin a group. Bumps the generation and recomputes assignment."""
        state = self._group(group_id)
        if protocol is not None:
            self._protocols[group_id] = protocol

        self._evict_expired(state)

        mid = member_id or f"member-{_uuid_hex()[:12]}"
        existing = state.members.get(mid)
        if existing is not None:
            existing.subscription = subscription
            existing.last_heartbeat = self._clock()
        else:
            state.members[mid] = GroupMember(
                member_id=mid,
                subscription=subscription,
                last_heartbeat=self._clock(),
            )
        self._rebalance(state)
        member = state.members[mid]
        return JoinResult(
            member_id=mid,
            generation=state.generation,
            assignment=member.assignment,
            is_leader=(state.leader == mid),
        )

    def leave(self, group_id: str, member_id: str) -> None:
        """Remove a member and rebalance the group's partitions."""
        state = self._group(group_id)
        if member_id not in state.members:
            raise UnknownMemberError(group_id, member_id)
        del state.members[member_id]
        self._rebalance(state)

    def heartbeat(self, group_id: str, member_id: str, generation: int) -> bool:
        """Record liveness; ``False`` means the member must rejoin (stale generation)."""
        state = self._group(group_id)
        member = state.members.get(member_id)
        if member is None:
            raise UnknownMemberError(group_id, member_id)
        member.last_heartbeat = self._clock()
        if generation != state.generation:
            return False
        # Lazily evict peers whose session timed out, then re-check our own.
        if self._evict_expired(state, protect=member_id):
            self._rebalance(state)
            return state.generation == generation
        return True

    def _evict_expired(self, state: GroupState, *, protect: str | None = None) -> bool:
        """Drop members past the session timeout; return whether any were removed."""
        if self._session_timeout_s <= 0:
            return False
        now = self._clock()
        dead = [
            mid
            for mid, m in state.members.items()
            if mid != protect and (now - m.last_heartbeat) > self._session_timeout_s
        ]
        for mid in dead:
            del state.members[mid]
        return bool(dead)

    def _rebalance(self, state: GroupState) -> None:
        """Recompute assignment for all members and bump the generation."""
        state.generation += 1
        if not state.members:
            state.leader = None
            return
        state.leader = sorted(state.members)[0]

        members_sub = {mid: m.subscription for mid, m in state.members.items()}
        topics = {t for subs in members_sub.values() for t in subs}
        counts = {t: self._partition_counts(t) for t in topics}
        current = {mid: m.assignment for mid, m in state.members.items()}

        assignment = self._assignor(state.group_id).assign(members_sub, counts, current)
        for mid, member in state.members.items():
            member.assignment = assignment.get(mid, ())

    def describe(self, group_id: str) -> GroupDescription:
        """Snapshot a group's membership + generation."""
        state = self._group(group_id)
        subs: set[str] = set()
        members = []
        for mid in sorted(state.members):
            m = state.members[mid]
            subs.update(m.subscription)
            members.append(MemberInfo(member_id=mid, assignment=m.assignment))
        return GroupDescription(
            group_id=group_id,
            generation=state.generation,
            members=tuple(members),
            subscription=tuple(sorted(subs)),
        )

    # --- offset store ---------------------------------------------------- #

    def commit(
        self,
        group_id: str,
        offsets: dict[TopicPartition, int],
        *,
        generation: int | None = None,
        member_id: str | None = None,
    ) -> None:
        """Persist committed offsets, fencing stale generations.

        A commit carrying a generation older than the group's current one is
        rejected (the member's assignment was revoked mid-flight) — Kafka's
        ``CommitFailedException``.
        """
        state = self._group(group_id)
        if generation is not None and generation < state.generation:
            raise CommitConflictError(group_id, generation, state.generation)
        if member_id is not None and member_id not in state.members and state.members:
            raise UnknownMemberError(group_id, member_id)
        state.committed.update(offsets)

    def fetch_committed(
        self, group_id: str, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int | None]:
        """Read committed offsets (``None`` where the group never committed)."""
        state = self._group(group_id)
        return {tp: state.committed.get(tp) for tp in partitions}

    def list_committed(self, group_id: str) -> dict[TopicPartition, int]:
        """All committed offsets for a group."""
        return dict(self._group(group_id).committed)

    def drop_topic(self, topic: str) -> None:
        """Forget all committed offsets for a deleted topic, across every group."""
        for state in self._groups.values():
            stale = [tp for tp in state.committed if tp.topic == topic]
            for tp in stale:
                del state.committed[tp]


def _default_clock() -> Callable[[], float]:
    import time

    return time.monotonic
