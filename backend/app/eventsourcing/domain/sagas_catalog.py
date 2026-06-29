"""Concrete saga triggers wiring the §9.7 loop and §5.4 canon-edit reactions.

These are the *process-manager rules* of the write side — pure functions that map
a committed fact to the follow-up command(s) it should cause:

* **render loop (§9.7):** a clip is ``ShotRendered`` -> score it (``ScoreShotQA``).
  (The QA verdict's own routing to Accept/Repair lives in the
  :func:`~app.eventsourcing.domain.handlers.handle_score_qa` handler, so it is a
  single atomic decision rather than two saga hops.)
* **director comment (§5.4):** a ``DirectorCommentLeft`` targeting a shot -> repair
  that shot (the REST regen path; the design note: a region comment POSTs to regen).
* **canon edit (§5.4):** a ``CanonFieldEdited`` / ``CanonReferenceImageSwapped`` /
  ``CanonEvolvedFromConflict`` -> repair each dependent shot, "surgical, not a full
  re-render".

The triggers only *decide* the follow-up commands; whether they run inline or get
enqueued is the composition root's choice (the dispatcher's sink). The QA-scoring
follow-up deliberately needs a verdict the saga cannot compute, so it is left to
the application layer to drive (it requires the Critic); these triggers cover the
deterministic, score-free reactions.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.eventsourcing.domain import commands_catalog as cc
from app.eventsourcing.domain.canon import (
    CanonEvolvedFromConflict,
    CanonFieldEdited,
    CanonReferenceImageSwapped,
)
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.events import DomainEvent, EventMetadata
from app.eventsourcing.domain.saga import SagaDispatcher
from app.eventsourcing.domain.session import DirectorCommentLeft


def on_director_comment(event: DomainEvent, _meta: EventMetadata) -> Sequence[Command]:
    """§5.4 REST regen path: a region comment on a shot triggers its re-render."""
    if not isinstance(event, DirectorCommentLeft):  # pragma: no cover - registered by type
        return ()
    if not event.shot_id:
        return ()
    return (
        cc.RegenerateShot(
            shot_id=event.shot_id,
            reason=event.note,
            triggered_by=f"comment:{event.comment_id}",
        ),
    )


def on_canon_edit(event: DomainEvent, _meta: EventMetadata) -> Sequence[Command]:
    """§5.4 surgical regen: re-render exactly the shots that depend on the edit."""
    dependent: tuple[str, ...]
    triggered_by: str
    if isinstance(event, CanonFieldEdited):
        dependent = event.dependent_shot_ids
        triggered_by = f"canon_edit:{event.entity_id}/{event.field_name}"
    elif isinstance(event, CanonReferenceImageSwapped):
        dependent = event.dependent_shot_ids
        triggered_by = f"canon_ref_swap:{event.entity_id}"
    elif isinstance(event, CanonEvolvedFromConflict):
        dependent = event.dependent_shot_ids
        triggered_by = f"canon_evolve:{event.entity_id}/{event.conflict_id}"
    else:  # pragma: no cover - registered by type
        return ()
    return tuple(
        cc.RegenerateShot(shot_id=shot_id, triggered_by=triggered_by) for shot_id in dependent
    )


def register_default_sagas(dispatcher: SagaDispatcher) -> SagaDispatcher:
    """Register the deterministic §9.7/§5.4 saga triggers on ``dispatcher``."""
    dispatcher.register(DirectorCommentLeft.event_type, on_director_comment)
    dispatcher.register(CanonFieldEdited.event_type, on_canon_edit)
    dispatcher.register(CanonReferenceImageSwapped.event_type, on_canon_edit)
    dispatcher.register(CanonEvolvedFromConflict.event_type, on_canon_edit)
    return dispatcher


__all__ = ["on_canon_edit", "on_director_comment", "register_default_sagas"]
