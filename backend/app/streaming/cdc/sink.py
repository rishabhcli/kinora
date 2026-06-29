"""Where change events go after the source emits them.

The CDC pipeline doesn't care *what* consumes its events — only that the thing
has an ``emit(event)`` coroutine. That contract is :class:`ChangeSink`.

Facet A (the streaming broker) may or may not exist on disk yet. Rather than
hard-import it, this module:

* defines the minimal :class:`ChangeSink` protocol the pipeline depends on,
* ships an :class:`InMemorySink` (deterministic test capture) and a
  :class:`RedisStreamSink` (production fan-out over the existing async Redis
  client's pub/sub), and
* provides :class:`BrokerSink`, a *duck-typed* adapter that wraps any object
  exposing a broker-shaped ``publish(topic, payload)`` so a sibling
  ``Broker`` can be injected the moment it lands, with no import coupling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from app.streaming.cdc.events import ChangeEvent


@runtime_checkable
class ChangeSink(Protocol):
    """The one method the CDC pipeline needs from any downstream consumer."""

    async def emit(self, event: ChangeEvent) -> None:
        """Accept one change event. Must be idempotent under at-least-once."""
        ...


@runtime_checkable
class BrokerLike(Protocol):
    """The shape of sibling facet A's ``Broker`` we depend on (structurally).

    We only need ``publish``; declaring it as a Protocol means any object with a
    compatible ``publish`` satisfies it without an import.
    """

    async def publish(self, topic: str, payload: Any) -> Any: ...


class InMemorySink:
    """Collects events in order — the deterministic test sink.

    Also usable as a tiny in-process bus: register callbacks via
    :meth:`on_emit` to react synchronously to each event.
    """

    def __init__(self) -> None:
        self.events: list[ChangeEvent] = []
        self._callbacks: list[Callable[[ChangeEvent], Awaitable[None] | None]] = []

    async def emit(self, event: ChangeEvent) -> None:
        self.events.append(event)
        for cb in self._callbacks:
            result = cb(event)
            if asyncio.iscoroutine(result):
                await result

    def on_emit(self, callback: Callable[[ChangeEvent], Awaitable[None] | None]) -> None:
        self._callbacks.append(callback)

    # -- ergonomic test helpers -------------------------------------------- #
    def for_table(self, table: str) -> list[ChangeEvent]:
        return [e for e in self.events if e.table == table]

    def clear(self) -> None:
        self.events.clear()

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.events)


class FanoutSink:
    """Broadcasts every event to a fixed set of child sinks (tee).

    Used to drive the materialised-view engine *and* an external broker from one
    source. A child raising does not stop its siblings; the first error is
    re-raised after all children have been attempted (at-least-once friendly).
    """

    def __init__(self, sinks: Iterable[ChangeSink]) -> None:
        self._sinks: list[ChangeSink] = list(sinks)

    def add(self, sink: ChangeSink) -> None:
        self._sinks.append(sink)

    async def emit(self, event: ChangeEvent) -> None:
        first_error: BaseException | None = None
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except BaseException as exc:  # noqa: BLE001 - tee must not short-circuit
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class BrokerSink:
    """Adapt a broker-shaped object (sibling facet A) into a :class:`ChangeSink`.

    Each change event is published to ``f"{prefix}.{table}"`` as its
    :meth:`ChangeEvent.to_dict` envelope. ``HEARTBEAT`` events (no table) go to
    ``f"{prefix}.__heartbeat__"``. Accepts any object with a coroutine
    ``publish(topic, payload)`` — no import of the broker package.
    """

    def __init__(self, broker: BrokerLike, *, prefix: str = "cdc") -> None:
        self._broker = broker
        self._prefix = prefix

    def _topic(self, event: ChangeEvent) -> str:
        table = event.table or "__heartbeat__"
        return f"{self._prefix}.{table}"

    async def emit(self, event: ChangeEvent) -> None:
        await self._broker.publish(self._topic(event), event.to_dict())


class RedisStreamSink:
    """Publish change events over the existing async Redis pub/sub client.

    Thin wrapper over :meth:`app.redis.client.RedisClient.publish` so live
    deployments get fan-out without depending on facet A. Channel layout matches
    :class:`BrokerSink`. The client is duck-typed (any ``publish`` coroutine).
    """

    def __init__(self, redis_client: Any, *, prefix: str = "cdc") -> None:
        self._redis = redis_client
        self._prefix = prefix

    async def emit(self, event: ChangeEvent) -> None:
        table = event.table or "__heartbeat__"
        await self._redis.publish(f"{self._prefix}.{table}", event.to_dict())


class NullSink:
    """Discards everything — for benchmarks and "source only" runs."""

    async def emit(self, event: ChangeEvent) -> None:  # noqa: D401 - no-op
        return None


__all__ = [
    "BrokerLike",
    "BrokerSink",
    "ChangeSink",
    "FanoutSink",
    "InMemorySink",
    "NullSink",
    "RedisStreamSink",
]
