"""Consumer-group machinery: assignment strategies + the group coordinator.

* :mod:`app.streaming.log.group.assignor` — pure partition-assignment strategies
  (range, round-robin, cooperative-sticky) shared by every broker.
* :mod:`app.streaming.log.group.coordinator` — the in-process group state machine
  (membership, generations, rebalance, offset commit) the in-memory and Redis
  brokers embed to back the ``Broker`` group methods.
"""

from __future__ import annotations

from app.streaming.log.group.assignor import (
    Assignor,
    CooperativeStickyAssignor,
    RangeAssignor,
    RoundRobinAssignor,
    get_assignor,
)
from app.streaming.log.group.coordinator import GroupCoordinator, GroupMember, GroupState

__all__ = [
    "Assignor",
    "CooperativeStickyAssignor",
    "GroupCoordinator",
    "GroupMember",
    "GroupState",
    "RangeAssignor",
    "RoundRobinAssignor",
    "get_assignor",
]
