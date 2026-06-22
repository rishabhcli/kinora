"""The per-shot §9.7 state machine: legal edges, persistence hook, projection."""

from __future__ import annotations

import pytest

from app.db.models.enums import ShotStatus
from app.render.states import (
    IllegalTransitionError,
    RenderState,
    ShotStateMachine,
    is_allowed,
    to_status,
)


async def test_happy_path_transitions_and_history() -> None:
    machine = ShotStateMachine("shot_1")
    assert machine.state is RenderState.PLANNED
    for nxt in (
        RenderState.KEYFRAMED,
        RenderState.PROMOTED,
        RenderState.CACHE_CHECK,
        RenderState.RENDERING,
        RenderState.QA,
        RenderState.ACCEPTED,
    ):
        await machine.transition(nxt)
    assert machine.is_terminal
    assert [e.dst for e in machine.history][-1] is RenderState.ACCEPTED


async def test_illegal_transition_raises() -> None:
    machine = ShotStateMachine("shot_2")
    with pytest.raises(IllegalTransitionError):
        await machine.transition(RenderState.ACCEPTED)  # Planned -> Accepted is illegal


async def test_repair_can_route_to_conflict_or_degrade() -> None:
    assert is_allowed(RenderState.QA, RenderState.REPAIR)
    assert is_allowed(RenderState.REPAIR, RenderState.CONFLICT)
    assert is_allowed(RenderState.REPAIR, RenderState.DEGRADED)
    assert is_allowed(RenderState.REPAIR, RenderState.RENDERING)
    # §7.2 arbitration/continuity-clear: Conflict -> Approved (Accepted).
    assert is_allowed(RenderState.CONFLICT, RenderState.ACCEPTED)
    assert is_allowed(RenderState.CONFLICT, RenderState.RENDERING)


async def test_transition_fires_persist_hook() -> None:
    persisted: list[tuple[RenderState, ShotStatus]] = []

    async def hook(state: RenderState, status: ShotStatus) -> None:
        persisted.append((state, status))

    machine = ShotStateMachine("shot_3", on_transition=hook)
    await machine.transition(RenderState.KEYFRAMED)
    await machine.transition(RenderState.PROMOTED)
    assert persisted == [
        (RenderState.KEYFRAMED, ShotStatus.KEYFRAMED),
        (RenderState.PROMOTED, ShotStatus.PROMOTED),
    ]


def test_status_projection() -> None:
    assert to_status(RenderState.CACHE_CHECK) is ShotStatus.PROMOTED
    assert to_status(RenderState.REPAIR) is ShotStatus.RENDERING
    assert to_status(RenderState.ACCEPTED) is ShotStatus.ACCEPTED
    assert to_status(RenderState.DEGRADED) is ShotStatus.DEGRADED
