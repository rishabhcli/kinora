"""The aggregate-root base — the unit of consistency on the write side.

An **aggregate** owns one event stream and enforces its invariants. The contract,
deliberately split into a *pure decision core* and a *pure mutation core*:

* **Rebuild** — :meth:`AggregateRoot.replay` folds a stream of past events into
  current state by dispatching each to a ``when(event)`` method (``apply`` for the
  generic hook). This is the *only* way state is reconstructed; there is no setter.
* **Decide** — a command handler on a concrete aggregate is a *pure function of
  ``(state, command)``* that validates invariants and returns the new events to
  emit (it never mutates ``self`` directly). It calls :meth:`emit`, which appends
  to :attr:`uncommitted` *and* immediately folds the event into ``self`` via
  :meth:`apply`, so a single command can make several decisions in a row, each
  seeing the effect of the last.

Because both halves are pure and synchronous, every aggregate is exhaustively
unit-testable with no store, no clock, and no event loop: build it from a list of
events, call a decision method, assert on the emitted events.

The :class:`Repository` (see :mod:`app.eventsourcing.domain.repository`) is the
only thing that touches the store; it loads events to rebuild an aggregate and
persists :attr:`uncommitted` with optimistic concurrency.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, TypeVar

from app.eventsourcing.domain.events import DomainEvent
from app.eventsourcing.domain.identifiers import StreamCategory, StreamId

#: The state-carrying aggregate subtype (for typed factory return values).
_A = TypeVar("_A", bound="AggregateRoot")


class AggregateRoot:
    """Base class for event-sourced aggregates.

    Subclasses:

    * set :attr:`category` (the :class:`~app.eventsourcing.domain.identifiers.StreamCategory`);
    * implement ``apply(event)`` to fold an event into ``self`` (typically a small
      dispatch on ``type(event)`` to ``when_*`` methods);
    * expose decision methods that validate then call :meth:`emit`.

    State invariants:

    * :attr:`version` is the stream version this instance reflects — 0 for a
      brand-new aggregate, bumped by 1 for every applied event (committed *or*
      uncommitted), so it always equals "events folded so far".
    * :attr:`uncommitted` holds events emitted since the last
      :meth:`mark_committed`; the repository appends exactly these.
    """

    #: Set by each concrete aggregate to its :class:`StreamCategory`.
    category: StreamCategory | None = None

    def __init__(self, aggregate_id: str) -> None:
        self.aggregate_id = aggregate_id
        self.version: int = 0
        self._uncommitted: list[DomainEvent] = []
        # ``_committed_version`` is the version of the last *persisted* event; the
        # repository asserts the store is at this version when it appends.
        self._committed_version: int = 0

    # -- rebuild ------------------------------------------------------------- #

    @property
    def uncommitted(self) -> tuple[DomainEvent, ...]:
        """Events emitted since the last commit (what the repository will append)."""
        return tuple(self._uncommitted)

    @property
    def has_uncommitted(self) -> bool:
        return bool(self._uncommitted)

    @property
    def expected_version(self) -> int:
        """The stream version the repository should assert when appending.

        This is the version of the last *committed* event — i.e. excluding the
        uncommitted ones — which is exactly the optimistic-concurrency token.
        """
        return self._committed_version

    def replay(self, events: Iterable[DomainEvent]) -> None:
        """Rebuild state by folding committed history (does not touch uncommitted).

        Each event bumps :attr:`version` *and* :attr:`_committed_version` because
        replayed events are, by definition, already persisted.
        """
        for event in events:
            self.apply(event)
            self.version += 1
            self._committed_version += 1

    # -- decide -> emit ------------------------------------------------------ #

    def emit(self, event: DomainEvent) -> DomainEvent:
        """Record a freshly-decided event: queue it and fold it into ``self``.

        Bumps :attr:`version` (so subsequent decisions in the same command see the
        new state) but **not** :attr:`_committed_version` (it is not persisted yet).
        Returns the event for convenient chaining/assertions.
        """
        self.apply(event)
        self._uncommitted.append(event)
        self.version += 1
        return event

    def mark_committed(self) -> None:
        """Clear uncommitted events after the repository has persisted them."""
        self._committed_version = self.version
        self._uncommitted.clear()

    # -- the fold ------------------------------------------------------------ #

    def apply(self, event: DomainEvent) -> None:
        """Fold one event into ``self``. Concrete aggregates override this.

        The conventional implementation dispatches on ``type(event)`` to a
        ``when_<event>`` method. Unknown events should be ignored (forward
        compatibility) or raise, at the aggregate's discretion.
        """
        raise NotImplementedError

    # -- identity ------------------------------------------------------------ #

    @property
    def stream_id(self) -> StreamId:
        """This aggregate's typed stream id."""
        cat = type(self).category
        if not isinstance(cat, StreamCategory):  # pragma: no cover - misuse
            raise TypeError(f"{type(self).__name__}.category must be a StreamCategory")
        return StreamId(cat, self.aggregate_id)

    @property
    def exists(self) -> bool:
        """Whether any event has been folded (committed or uncommitted)."""
        return self.version > 0


class _Dispatcher(Generic[_A]):
    """A tiny helper concrete aggregates may use for ``apply`` dispatch.

    Not required — aggregates may hand-roll their ``apply`` — but it keeps the
    ``when_*`` registration declarative and avoids a long ``if isinstance`` ladder.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], str] = {}

    def on(self, event_type: type[DomainEvent], method_name: str) -> None:
        self._handlers[event_type] = method_name

    def dispatch(self, agg: _A, event: DomainEvent) -> None:
        method_name = self._handlers.get(type(event))
        if method_name is None:
            return  # forward-compatible: ignore unknown events
        getattr(agg, method_name)(event)


__all__ = ["AggregateRoot"]
