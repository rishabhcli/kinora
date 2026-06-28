"""The DSAR workflow state machine (pure transition logic).

States and the legal transitions between them (GDPR Art. 12 lifecycle):

```
received ──▶ verifying ──▶ in_progress ──▶ completed
   │             │              │   ▲
   │             │              │   └── extended (Art. 12(3) one-month extension)
   │             ▼              ▼
   └──────────▶ rejected     rejected
   │
   └──▶ cancelled  (subject withdraws; allowed from any non-terminal state)
```

This module is pure (no DB) so the transition table is exhaustively unit-testable;
:class:`~app.compliance.dsar.service.DSARService` applies it against the DB.
"""

from __future__ import annotations

from app.compliance.enums import DSARState
from app.compliance.errors import InvalidTransitionError

#: States from which no further transition is allowed.
TERMINAL_STATES: frozenset[DSARState] = frozenset(
    {DSARState.COMPLETED, DSARState.REJECTED, DSARState.CANCELLED}
)

#: The legal forward transitions (cancellation is handled separately because it
#: is allowed from *any* non-terminal state).
_TRANSITIONS: dict[DSARState, frozenset[DSARState]] = {
    DSARState.RECEIVED: frozenset({DSARState.VERIFYING, DSARState.REJECTED}),
    DSARState.VERIFYING: frozenset({DSARState.IN_PROGRESS, DSARState.REJECTED}),
    DSARState.IN_PROGRESS: frozenset({DSARState.EXTENDED, DSARState.COMPLETED, DSARState.REJECTED}),
    DSARState.EXTENDED: frozenset({DSARState.COMPLETED, DSARState.REJECTED}),
    DSARState.COMPLETED: frozenset(),
    DSARState.REJECTED: frozenset(),
    DSARState.CANCELLED: frozenset(),
}


def is_terminal(state: DSARState) -> bool:
    """True when ``state`` admits no further transition."""
    return state in TERMINAL_STATES


def can_transition(current: DSARState, target: DSARState) -> bool:
    """True iff ``current → target`` is a legal DSAR transition."""
    if current == target:
        return False
    # Cancellation is permitted from any non-terminal state.
    if target == DSARState.CANCELLED:
        return not is_terminal(current)
    return target in _TRANSITIONS.get(current, frozenset())


def allowed_next(current: DSARState) -> frozenset[DSARState]:
    """The set of states reachable from ``current`` in one step."""
    if is_terminal(current):
        return frozenset()
    nxt = set(_TRANSITIONS.get(current, frozenset()))
    nxt.add(DSARState.CANCELLED)
    return frozenset(nxt)


class DSARMachine:
    """A thin, stateless validator over the DSAR transition table."""

    @staticmethod
    def assert_transition(current: DSARState, target: DSARState) -> None:
        """Raise :class:`InvalidTransitionError` unless the transition is legal."""
        if not can_transition(current, target):
            raise InvalidTransitionError(
                f"illegal DSAR transition {current.value!r} → {target.value!r}"
            )

    @staticmethod
    def allowed_next(current: DSARState) -> frozenset[DSARState]:
        """The legal next states from ``current``."""
        return allowed_next(current)


__all__ = [
    "TERMINAL_STATES",
    "DSARMachine",
    "allowed_next",
    "can_transition",
    "is_terminal",
]
