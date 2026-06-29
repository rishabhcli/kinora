"""The command bus — the CQRS write-side entry point.

The bus is the single front door for changing state. It:

1. routes a :class:`Command` to its registered **handler** by ``command_type``;
2. runs the command through the composed **middleware** pipeline (validation →
   auth seam → idempotency → logging → handler) — each a separable concern;
3. wraps the handler in the **optimistic-concurrency retry** loop, so a losing
   writer re-loads and re-decides rather than clobbering a winner;
4. stamps each emitted event with provenance metadata (a fresh ``event_id``, the
   ``occurred_at`` clock reading, and the command's causation/correlation ids)
   before persisting;
5. fans the committed events out to the registered **saga triggers**, the seam
   that turns a fact (``ShotRendered``) into the next command (``RunShotQA``).

A *handler* is ``async (command, repo) -> AggregateRoot``: it loads/builds the
target aggregate, calls its pure decision method, and returns it with its
uncommitted events queued. The bus owns the append + metadata + sagas so handlers
stay tiny and the decision logic stays in the aggregates.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.logging import get_logger
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.commands import Command, CommandResult
from app.eventsourcing.domain.concurrency import RetryPolicy, Sleeper, retry_on_conflict
from app.eventsourcing.domain.events import (
    DomainEvent,
    EventMetadata,
    now_utc,
)
from app.eventsourcing.domain.middleware import (
    CommandContext,
    Middleware,
    NextHandler,
)
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.saga import SagaDispatcher
from app.eventsourcing.store.protocol import EventStore

logger = get_logger("app.eventsourcing.domain.bus")

#: A command handler: load/build the aggregate, decide, return it (events queued).
CommandHandler = Callable[[Command, Repository[AggregateRoot]], Awaitable[AggregateRoot]]

#: A clock the bus reads for ``occurred_at`` (injectable for deterministic tests).
Clock = Callable[[], datetime]

#: Generates event ids (injectable so tests can assert deterministic ids).
IdFactory = Callable[[], str]

#: An async sink the bus pushes each batch of committed ``(event, metadata)`` to,
#: so read-model projections update inline (the CQRS query-side fan-out).
ProjectionSink = Callable[[Sequence[tuple[DomainEvent, EventMetadata]]], Awaitable[None]]


def _uuid_hex() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class _Registration:
    handler: CommandHandler
    repository: Repository[AggregateRoot]


@dataclass(slots=True)
class _Committed:
    """The outcome of one (possibly retried) load-decide-append operation."""

    aggregate: AggregateRoot
    events: Sequence[DomainEvent]
    metadata: Sequence[EventMetadata]
    new_version: int


@dataclass(slots=True)
class CommandBus:
    """Routes commands to handlers through middleware, with concurrency retries.

    Args:
        store: the event store (facet A) — handed to repositories.
        middleware: the ordered pipeline (outermost first). The bus appends its
            own terminal stage that runs the routed handler.
        retry_policy: the optimistic-concurrency backoff schedule.
        sagas: the saga dispatcher fired with committed events post-append.
        projection_sink: optional CQRS read-side fan-out; receives each committed
            ``(event, metadata)`` batch so read models update inline.
        clock / id_factory / sleeper: injectable for deterministic tests.
    """

    store: EventStore
    middleware: list[Middleware] = field(default_factory=list)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    sagas: SagaDispatcher = field(default_factory=SagaDispatcher)
    projection_sink: ProjectionSink | None = None
    clock: Clock = now_utc
    id_factory: IdFactory = _uuid_hex
    sleeper: Sleeper | None = None
    _registry: dict[str, _Registration] = field(default_factory=dict)

    def register(
        self,
        command_type: str,
        handler: CommandHandler,
        repository: Repository[AggregateRoot],
    ) -> None:
        """Register the handler + repository for a command type."""
        if command_type in self._registry:
            raise ValueError(f"command {command_type!r} already has a handler")
        self._registry[command_type] = _Registration(handler, repository)

    async def dispatch(
        self,
        command: Command,
        *,
        metadata: EventMetadata | None = None,
    ) -> CommandResult:
        """Handle a command end-to-end and return its :class:`CommandResult`.

        Raises:
            KeyError: no handler is registered for ``command.command_type``.
            DomainError: a business-rule rejection from validation/auth/decision.
            ConcurrencyError: a write conflict that outlived the retry policy.
        """
        ctx = CommandContext(command=command, metadata=metadata or EventMetadata())
        terminal: NextHandler = self._run_handler
        pipeline = terminal
        # Compose middleware as an onion (outermost wraps last so it runs first).
        for mw in reversed(self.middleware):
            pipeline = _bind(mw, pipeline)
        result = await pipeline(ctx)
        return result

    def _registration_for(self, command: Command) -> _Registration:
        try:
            return self._registry[command.command_type]
        except KeyError as exc:
            raise KeyError(f"no handler registered for command {command.command_type!r}") from exc

    async def _run_handler(self, ctx: CommandContext) -> CommandResult:
        command = ctx.command
        registration = self._registration_for(command)
        handler = registration.handler
        repo = registration.repository
        stream_id = command.target_stream()

        async def operation() -> _Committed:
            aggregate = await handler(command, repo)
            pending = aggregate.uncommitted
            if not pending:
                version = await repo.current_version(aggregate.stream_id)
                return _Committed(aggregate, (), (), version)
            per_event_meta = self._stamp(ctx.metadata, len(pending))
            append = await repo.save_with_metadata(aggregate, per_event_meta)
            return _Committed(aggregate, pending, tuple(per_event_meta), append.last_version)

        committed = await retry_on_conflict(
            operation,
            policy=self.retry_policy,
            sleeper=self.sleeper,
        )
        aggregate, events, new_version = (
            committed.aggregate,
            committed.events,
            committed.new_version,
        )

        if events:
            # Sagas + projections observe *committed* facts only, post-append.
            await self.sagas.dispatch(events, source_metadata=ctx.metadata)
            if self.projection_sink is not None:
                await self.projection_sink(list(zip(events, committed.metadata, strict=True)))

        return CommandResult(
            stream_id=aggregate.stream_id if events else stream_id,
            events=events,
            new_version=new_version,
        )

    def _stamp(self, base: EventMetadata, count: int) -> list[EventMetadata]:
        """Produce per-event metadata: fresh ids + clock, inheriting provenance."""
        occurred = self.clock()
        causation = base.causation_id or base.correlation_id
        out: list[EventMetadata] = []
        for _ in range(count):
            out.append(
                EventMetadata(
                    event_id=self.id_factory(),
                    occurred_at=occurred,
                    actor_id=base.actor_id,
                    correlation_id=base.correlation_id,
                    causation_id=causation,
                    tenant_id=base.tenant_id,
                )
            )
        return out


def _bind(mw: Middleware, next_: NextHandler) -> NextHandler:
    async def call(ctx: CommandContext) -> CommandResult:
        return await mw.handle(ctx, next_)

    return call


__all__ = ["Clock", "CommandBus", "CommandHandler", "IdFactory"]
