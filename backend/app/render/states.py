"""The per-shot state machine (kinora.md §9.7).

The §9.7 diagram is encoded here as an explicit, validated state machine so the
Phase-B orchestrator (:mod:`app.render.pipeline`) can log every transition and
persist a coarse status, and so the legal edges are unit-testable in isolation.

``RenderState`` is the *fine-grained* conceptual state from the §9.7 diagram
(including ``CACHE_CHECK`` and ``REPAIR``, which have no dedicated row status).
:func:`to_status` projects it onto the persisted :class:`app.db.models.enums.ShotStatus`
so the database stays simple while the transition log keeps full fidelity.

The machine is deliberately persistence-agnostic: it records transitions and
invokes an optional ``on_transition`` hook (the pipeline wires that to
``ShotRepo.set_status``), so this module imports no repository and is free of any
DB/event-loop coupling for tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.db.models.enums import ShotStatus

logger = get_logger("app.render.states")


class RenderState(StrEnum):
    """The §9.7 per-shot states (fine-grained; a superset of ``ShotStatus``)."""

    PLANNED = "planned"
    KEYFRAMED = "keyframed"
    PROMOTED = "promoted"
    CACHE_CHECK = "cache_check"
    RENDERING = "rendering"
    QA = "qa"
    REPAIR = "repair"
    CONFLICT = "conflict"
    ACCEPTED = "accepted"
    DEGRADED = "degraded"


#: Terminal states: the render call ends here (§9.7 — ``Accepted``/``Degraded``
#: sink, and a surfaced ``Conflict`` awaits the director out-of-band).
TERMINAL_STATES: frozenset[RenderState] = frozenset({RenderState.ACCEPTED, RenderState.DEGRADED})

#: The legal edges of the §9.7 diagram. Any transition not listed raises.
ALLOWED_TRANSITIONS: dict[RenderState, frozenset[RenderState]] = {
    RenderState.PLANNED: frozenset({RenderState.KEYFRAMED, RenderState.PROMOTED}),
    RenderState.KEYFRAMED: frozenset({RenderState.PROMOTED}),
    RenderState.PROMOTED: frozenset({RenderState.CACHE_CHECK}),
    RenderState.CACHE_CHECK: frozenset({RenderState.ACCEPTED, RenderState.RENDERING}),
    RenderState.RENDERING: frozenset({RenderState.QA, RenderState.DEGRADED}),
    RenderState.QA: frozenset({RenderState.ACCEPTED, RenderState.REPAIR}),
    RenderState.REPAIR: frozenset(
        {RenderState.RENDERING, RenderState.CONFLICT, RenderState.DEGRADED}
    ),
    RenderState.CONFLICT: frozenset(
        # §7.2: honor/evolve -> regen (Rendering); arbitration/continuity-clear ->
        # Approved (Accepted); surfaced conflicts stay parked in CONFLICT.
        {
            RenderState.RENDERING,
            RenderState.ACCEPTED,
            RenderState.DEGRADED,
            RenderState.CONFLICT,
        }
    ),
    RenderState.ACCEPTED: frozenset(),
    RenderState.DEGRADED: frozenset(),
}

#: Projection of a fine-grained state onto the persisted row status.
_STATUS_MAP: dict[RenderState, ShotStatus] = {
    RenderState.PLANNED: ShotStatus.PLANNED,
    RenderState.KEYFRAMED: ShotStatus.KEYFRAMED,
    RenderState.PROMOTED: ShotStatus.PROMOTED,
    # No dedicated row status: the cache probe is the moment just before render.
    RenderState.CACHE_CHECK: ShotStatus.PROMOTED,
    RenderState.RENDERING: ShotStatus.RENDERING,
    RenderState.QA: ShotStatus.QA,
    # A repair re-enters rendering; persist it as RENDERING.
    RenderState.REPAIR: ShotStatus.RENDERING,
    RenderState.CONFLICT: ShotStatus.CONFLICT,
    RenderState.ACCEPTED: ShotStatus.ACCEPTED,
    RenderState.DEGRADED: ShotStatus.DEGRADED,
}


def to_status(state: RenderState) -> ShotStatus:
    """Project a §9.7 ``RenderState`` onto the persisted :class:`ShotStatus`."""
    return _STATUS_MAP[state]


def is_allowed(src: RenderState, dst: RenderState) -> bool:
    """Return whether ``src -> dst`` is a legal §9.7 edge."""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


class IllegalTransitionError(RuntimeError):
    """Raised when an out-of-spec state transition is attempted."""

    def __init__(self, src: RenderState, dst: RenderState) -> None:
        self.src = src
        self.dst = dst
        super().__init__(f"illegal shot transition {src.value} -> {dst.value}")


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    """One recorded state transition (for logging + post-hoc inspection)."""

    src: RenderState
    dst: RenderState


#: Optional async hook fired on each transition (the pipeline persists status).
OnTransition = Callable[[RenderState, ShotStatus], Awaitable[None]]


@dataclass(slots=True)
class ShotStateMachine:
    """A validated §9.7 state machine with a transition log and a persist hook.

    Args:
        shot_id: the shot this machine tracks (for structured logs).
        state: the starting state (defaults to ``PLANNED``).
        on_transition: optional async hook invoked after each legal transition —
            the pipeline wires this to ``ShotRepo.set_status(shot_id, status)``.
    """

    shot_id: str
    state: RenderState = RenderState.PLANNED
    on_transition: OnTransition | None = None
    history: list[TransitionEvent] = field(default_factory=list)

    async def transition(self, dst: RenderState) -> RenderState:
        """Move to ``dst`` if the edge is legal; log it and fire the persist hook.

        Raises:
            IllegalTransitionError: when ``state -> dst`` is not a §9.7 edge.
        """
        if dst == self.state:
            return self.state
        if not is_allowed(self.state, dst):
            raise IllegalTransitionError(self.state, dst)
        src = self.state
        self.state = dst
        self.history.append(TransitionEvent(src=src, dst=dst))
        logger.info(
            "shot.transition",
            shot_id=self.shot_id,
            **{"from": src.value, "to": dst.value},
        )
        if self.on_transition is not None:
            await self.on_transition(dst, to_status(dst))
        return dst

    @property
    def is_terminal(self) -> bool:
        """True once the shot has reached a sink state (§9.7)."""
        return self.state in TERMINAL_STATES


__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "IllegalTransitionError",
    "OnTransition",
    "RenderState",
    "ShotStateMachine",
    "TransitionEvent",
    "is_allowed",
    "to_status",
]
