"""Choreography mode — event-driven coordination without a central driver.

Where orchestration has one component (:class:`SagaOrchestrator`) that *tells*
each step to run, choreography has **no central brain**: each service reacts to an
event and emits the next event, and the saga's progress is an emergent property of
the event stream. This module provides the minimal, durable machinery to run a
choreographed saga correctly:

* :class:`ChoreographyEvent` — an immutable event carrying a ``type``, a
  ``correlation_id`` (which instance it belongs to), a monotonic ``sequence``, an
  ``idempotency_key`` (so a redelivered event is processed once), and a JSON
  ``payload``.
* :class:`EventBus` — the durable transport protocol: ``publish`` (append, deduped
  by idempotency key) and ``poll`` (read undelivered events for the subscribed
  types). :class:`InMemoryEventBus` is the reference; a Redis-stream / Postgres
  outbox bus slots in behind the same protocol.
* :class:`Reaction` — a registered reactor: *on event type X, run this handler*.
  The handler returns the events it wants emitted next (often zero or one); a
  handler that raises is retried with the bus's redelivery, and the effect ledger
  keeps its side effects exactly-once.
* :class:`ProcessManager` — drives the loop: poll the bus, dispatch each event to
  its reactions (deduping via the idempotency key + the effect ledger), publish
  the emitted follow-on events, and track per-correlation completion. It is the
  bridge that lets a *choreographed* saga still be observed and reasoned about as a
  single instance, and it reuses the same :class:`EffectLedger` for exactly-once.

Choreography vs. orchestration is a real engineering trade-off (loose coupling and
no single bottleneck vs. harder global reasoning); offering both behind shared
correctness primitives is the point of facet C.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Protocol, runtime_checkable

import structlog

from app.distributed.sagas.effects import EffectLedger, InMemoryEffectLedger
from app.jobs.clock import Clock, SystemClock

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChoreographyEvent:
    """An immutable event in a choreographed saga's stream."""

    type: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: int = 0
    created_at: datetime | None = None

    def derive(self, type_: str, **payload: Any) -> ChoreographyEvent:
        """Build a follow-on event in the same correlation, deriving a stable key.

        The derived idempotency key is deterministic in the parent event + the new
        type, so a reactor that re-runs (retry/crash) emits the *same* follow-on
        key and the bus dedups it — exactly-once propagation without a central
        sequencer.
        """
        return ChoreographyEvent(
            type=type_,
            correlation_id=self.correlation_id,
            payload=dict(payload),
            idempotency_key=f"{self.idempotency_key}->{type_}",
        )


@runtime_checkable
class EventBus(Protocol):
    """Durable append + poll transport for choreography events."""

    async def publish(self, event: ChoreographyEvent) -> bool:
        """Append ``event`` (deduped by idempotency key). Returns whether it was new."""
        ...

    async def poll(
        self, *, types: set[str] | None = None, limit: int = 100
    ) -> list[ChoreographyEvent]:
        """Return undelivered events (optionally filtered to ``types``)."""
        ...

    async def ack(self, event: ChoreographyEvent) -> None:
        """Mark ``event`` delivered so it is not polled again."""
        ...


class InMemoryEventBus:
    """A single-process reference :class:`EventBus` (append log + delivery cursor).

    The log is an append-only list deduped by idempotency key; ``poll`` returns
    not-yet-acked events. Durable within a process — which is what the
    deterministic tests need (a "crash" drops the process manager but keeps the
    bus, so unacked events are redelivered on resume).
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._log: list[ChoreographyEvent] = []
        self._keys: set[str] = set()
        self._acked: set[str] = set()
        self._seq = 0
        self._lock = asyncio.Lock()

    async def publish(self, event: ChoreographyEvent) -> bool:
        async with self._lock:
            if event.idempotency_key in self._keys:
                return False
            self._seq += 1
            stored = ChoreographyEvent(
                type=event.type,
                correlation_id=event.correlation_id,
                payload=dict(event.payload),
                idempotency_key=event.idempotency_key,
                sequence=self._seq,
                created_at=self._clock.now(),
            )
            self._log.append(stored)
            self._keys.add(event.idempotency_key)
            return True

    async def poll(
        self, *, types: set[str] | None = None, limit: int = 100
    ) -> list[ChoreographyEvent]:
        async with self._lock:
            out: list[ChoreographyEvent] = []
            for ev in self._log:
                if ev.idempotency_key in self._acked:
                    continue
                if types is not None and ev.type not in types:
                    continue
                out.append(ev)
                if len(out) >= limit:
                    break
            return out

    async def ack(self, event: ChoreographyEvent) -> None:
        async with self._lock:
            self._acked.add(event.idempotency_key)

    @property
    def events(self) -> list[ChoreographyEvent]:
        """All published events in order (test introspection)."""
        return list(self._log)

    def events_for(self, correlation_id: str) -> list[ChoreographyEvent]:
        """Events belonging to one correlation, in sequence order."""
        return [e for e in self._log if e.correlation_id == correlation_id]


#: A reaction handler: given the triggering event + an effect ledger, return the
#: events to emit next (possibly empty).
ReactionHandler = Callable[
    [ChoreographyEvent, EffectLedger], Awaitable["list[ChoreographyEvent] | None"]
]


@dataclass(frozen=True, slots=True)
class Reaction:
    """*On event type ``on``, run ``handler``* — one edge in the choreography graph."""

    on: str
    handler: ReactionHandler
    name: str = ""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What one :meth:`ProcessManager.drain` pass did (for tests + observability)."""

    processed: int
    emitted: int
    failed: int


class ProcessManager:
    """Drives a choreographed saga: poll → dispatch → emit, with exactly-once.

    Reactions are registered up front; :meth:`drain` repeatedly polls the bus for
    events any reaction subscribes to, dispatches each (guarded by the effect
    ledger so a redelivered or replayed event runs its reactor's side effects at
    most once), publishes the emitted follow-on events, and acks the input. A
    terminal event type (registered via ``complete_on``) marks a correlation
    finished so callers can await it.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        effects: EffectLedger | None = None,
        clock: Clock | None = None,
        complete_on: set[str] | None = None,
    ) -> None:
        self._bus = bus
        self._effects = effects or InMemoryEffectLedger(clock=clock)
        self._clock = clock or SystemClock()
        self._reactions: dict[str, list[Reaction]] = {}
        self._complete_on = set(complete_on or set())
        self._completed: dict[str, ChoreographyEvent] = {}

    def on(self, event_type: str, handler: ReactionHandler, *, name: str = "") -> None:
        """Register a reaction to ``event_type``."""
        self._reactions.setdefault(event_type, []).append(
            Reaction(on=event_type, handler=handler, name=name or handler.__name__)
        )

    def react(
        self, event_type: str, *, name: str = ""
    ) -> Callable[[ReactionHandler], ReactionHandler]:
        """Decorator form of :meth:`on`."""

        def deco(fn: ReactionHandler) -> ReactionHandler:
            self.on(event_type, fn, name=name)
            return fn

        return deco

    @property
    def subscribed_types(self) -> set[str]:
        return set(self._reactions)

    async def emit(self, event: ChoreographyEvent) -> bool:
        """Publish a (possibly seed) event onto the bus."""
        return await self._bus.publish(event)

    def is_complete(self, correlation_id: str) -> bool:
        """Whether a terminal event has been observed for ``correlation_id``."""
        return correlation_id in self._completed

    def result_for(self, correlation_id: str) -> ChoreographyEvent | None:
        """The terminal event for ``correlation_id`` (``None`` if not finished)."""
        return self._completed.get(correlation_id)

    async def drain(self, *, max_passes: int = 10_000) -> DispatchResult:
        """Process events until the bus has no more subscribed, unacked events.

        Each pass polls, dispatches, and publishes; loops until a poll returns
        nothing. Bounded by ``max_passes`` against a runaway emit cycle.
        """
        processed = emitted = failed = 0
        for _ in range(max_passes):
            events = await self._bus.poll(types=self.subscribed_types | self._complete_on)
            if not events:
                break
            made_progress = False
            for event in events:
                if event.type in self._complete_on:
                    self._completed[event.correlation_id] = event
                    await self._bus.ack(event)
                    made_progress = True
                    continue
                ok, n_emitted = await self._dispatch(event)
                if ok:
                    processed += 1
                    emitted += n_emitted
                    await self._bus.ack(event)
                    made_progress = True
                else:
                    failed += 1
            if not made_progress:
                break
        return DispatchResult(processed=processed, emitted=emitted, failed=failed)

    async def _dispatch(self, event: ChoreographyEvent) -> tuple[bool, int]:
        reactions = self._reactions.get(event.type, [])
        emitted = 0
        all_ok = True
        for reaction in reactions:
            key = f"choreo:{event.correlation_id}:{reaction.name}:{event.idempotency_key}"
            try:
                follow_ups = await self._effects.once(
                    key, partial(self._run_reaction, reaction, event)
                )
            except Exception:  # noqa: BLE001
                _log.exception(
                    "reaction failed",
                    event=event.type,
                    reaction=reaction.name,
                    correlation=event.correlation_id,
                )
                all_ok = False
                continue
            for spec in follow_ups or []:
                follow = ChoreographyEvent(
                    type=spec["type"],
                    correlation_id=event.correlation_id,
                    payload=spec.get("payload", {}),
                    idempotency_key=spec["idempotency_key"],
                )
                if await self._bus.publish(follow):
                    emitted += 1
        return all_ok, emitted

    async def _run_reaction(
        self, reaction: Reaction, event: ChoreographyEvent
    ) -> list[dict[str, Any]]:
        # Returns a JSON-serialisable description of the follow-on events so the
        # effect ledger can persist it (the ledger stores the *result* so a replay
        # re-emits the same events without re-running the handler body).
        result = await reaction.handler(event, self._effects)
        return [
            {
                "type": ev.type,
                "payload": ev.payload,
                "idempotency_key": ev.idempotency_key,
            }
            for ev in (result or [])
        ]


__all__ = [
    "ChoreographyEvent",
    "DispatchResult",
    "EventBus",
    "InMemoryEventBus",
    "ProcessManager",
    "Reaction",
    "ReactionHandler",
]
