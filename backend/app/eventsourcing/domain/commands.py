"""Commands — the *intent* half of CQRS, and the handler contract.

A **command** is an imperative request to change state: ``StartSession``,
``RenderShot``, ``EditCanonField``. Unlike events (facts in the past tense),
commands are in the imperative and *may be rejected*. Each is a frozen dataclass
carrying the target aggregate id and the data the decision needs.

A **command handler** is a pure function that loads (or builds) the target
aggregate, calls its decision method, and returns the aggregate so the bus can
persist its uncommitted events. The handler does no IO except through the
injected repository; the decision itself is the aggregate's pure method.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from app.eventsourcing.domain.events import DomainEvent
from app.eventsourcing.domain.identifiers import StreamId


@dataclass(frozen=True, slots=True)
class Command:
    """Base class for write-side commands.

    Subclasses are ``@dataclass(frozen=True, slots=True)`` and must expose the
    target aggregate id via :meth:`target_stream`. They may declare an
    ``idempotency_key`` field (or override :attr:`idempotency_key`) so the
    idempotency middleware can dedupe retried submissions.
    """

    #: Stable discriminator (defaults to the class name), used by the bus router.
    command_type: ClassVar[str] = "Command"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "command_type" not in cls.__dict__:
            cls.command_type = cls.__name__

    def target_stream(self) -> StreamId:
        """The stream id of the aggregate this command targets. Subclasses set this."""
        raise NotImplementedError

    @property
    def idempotency_key(self) -> str | None:
        """An optional dedupe key. Defaults to a declared ``idempotency_key`` field."""
        return getattr(self, "_idempotency_key", None)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of dispatching a command through the bus.

    Attributes:
        stream_id: the aggregate stream that was (or would be) written.
        events: the domain events the command produced (empty for a no-op /
            idempotent replay).
        new_version: the stream version after the append (== before, for a no-op).
        idempotent_replay: ``True`` when the bus short-circuited because this
            command had already been handled (idempotency middleware).
    """

    stream_id: StreamId
    events: Sequence[DomainEvent] = field(default_factory=tuple)
    new_version: int = 0
    idempotent_replay: bool = False

    @property
    def event_types(self) -> tuple[str, ...]:
        return tuple(e.event_type for e in self.events)


__all__ = ["Command", "CommandResult"]
