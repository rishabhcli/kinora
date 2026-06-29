"""The saga-trigger seam — turning committed facts into the next command.

A **saga** (a.k.a. a process manager) reacts to a domain event and decides what
should happen next, *as a command*. The §9.7 loop is full of these reactions:
``ShotRendered`` should trigger ``RunShotQA``; a failed QA should trigger a
``RepairShot`` or escalate to a ``RaiseConflict``; ``SessionEnded`` should trigger
preference-write-back (§9.6).

This module is deliberately only the **seam**: a :class:`SagaTrigger` maps an
event to zero-or-more follow-up commands, and a :class:`SagaDispatcher` runs the
registered triggers over the committed events the bus just appended. The actual
re-dispatch of those commands is the *consumer's* job — composition decides
whether they run inline (synchronously, same bus) or are enqueued — because the
domain layer must not own the scheduler/queue. The dispatcher therefore *returns*
the triggered commands and also forwards them to an optional sink callback, so
both styles are possible without a policy decision baked in here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.eventsourcing.domain.commands import Command
from app.eventsourcing.domain.events import DomainEvent, EventMetadata

logger = get_logger("app.eventsourcing.domain.saga")

#: A saga trigger: given a committed event (+ its provenance), return the
#: follow-up commands it should cause (empty when it does not react).
SagaTrigger = Callable[[DomainEvent, EventMetadata], Sequence[Command]]

#: An optional async sink the dispatcher forwards triggered commands to (e.g.
#: re-dispatch on the same bus, or enqueue). Receives one command + its
#: correlation metadata at a time.
SagaSink = Callable[[Command, EventMetadata], Awaitable[None]]


@dataclass(slots=True)
class TriggeredCommand:
    """A command a saga decided to cause, with the metadata to carry it forward."""

    command: Command
    metadata: EventMetadata


@dataclass(slots=True)
class SagaDispatcher:
    """Runs registered :data:`SagaTrigger` callbacks over committed events.

    Triggers are registered per event type (by ``event_type`` string) or globally
    (``"*"``). The dispatcher collects every triggered command, carries forward a
    causation chain (each follow-up's ``causation_id`` becomes the originating
    event's id, preserving the correlation id), and optionally forwards them to a
    :data:`SagaSink`. It returns the list so an inline consumer can re-dispatch.
    """

    triggers: dict[str, list[SagaTrigger]] = field(default_factory=dict)
    sink: SagaSink | None = None

    def register(self, event_type: str, trigger: SagaTrigger) -> None:
        """Register ``trigger`` for a specific event type (or ``"*"`` for all)."""
        self.triggers.setdefault(event_type, []).append(trigger)

    def on(self, event_type: str) -> Callable[[SagaTrigger], SagaTrigger]:
        """Decorator form of :meth:`register`."""

        def deco(trigger: SagaTrigger) -> SagaTrigger:
            self.register(event_type, trigger)
            return trigger

        return deco

    async def dispatch(
        self,
        events: Sequence[DomainEvent],
        *,
        source_metadata: EventMetadata,
    ) -> list[TriggeredCommand]:
        """Run triggers over ``events`` and return (and optionally sink) the result."""
        triggered: list[TriggeredCommand] = []
        for event in events:
            for trigger in self._triggers_for(event.event_type):
                for command in trigger(event, source_metadata):
                    meta = self._follow_metadata(source_metadata)
                    triggered.append(TriggeredCommand(command, meta))

        if self.sink is not None:
            for tc in triggered:
                logger.info(
                    "saga.trigger",
                    command=tc.command.command_type,
                    correlation_id=tc.metadata.correlation_id,
                )
                await self.sink(tc.command, tc.metadata)
        return triggered

    def _triggers_for(self, event_type: str) -> list[SagaTrigger]:
        return [*self.triggers.get(event_type, ()), *self.triggers.get("*", ())]

    @staticmethod
    def _follow_metadata(source: EventMetadata) -> EventMetadata:
        # A follow-up command inherits the correlation id (same logical workflow)
        # and points its causation at the event that triggered it.
        return EventMetadata(
            actor_id="saga",
            correlation_id=source.correlation_id or source.event_id,
            causation_id=source.event_id,
            tenant_id=source.tenant_id,
        )


__all__ = [
    "SagaDispatcher",
    "SagaSink",
    "SagaTrigger",
    "TriggeredCommand",
]
