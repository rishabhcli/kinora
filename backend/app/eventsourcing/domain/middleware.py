"""Command-bus middleware — the cross-cutting pipeline around every command.

Middleware wraps command handling as an onion: each layer receives the
:class:`CommandContext` and a ``next`` callable, may inspect/validate/short-circuit,
and otherwise calls ``next`` to proceed inward. The bus composes a list of
middleware around the terminal handler so concerns stay orthogonal:

* :class:`ValidationMiddleware` — structural validation (pure, per-command rules);
* :class:`AuthorizationMiddleware` — the auth seam (a pluggable policy callback);
* :class:`IdempotencyMiddleware` — dedupe retried submissions by idempotency key;
* :class:`LoggingMiddleware` — structured before/after logging (no business logic).

Every middleware is independently unit-testable: feed it a context + a stub
``next`` and assert on what it calls/raises.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger
from app.eventsourcing.domain.commands import Command, CommandResult
from app.eventsourcing.domain.errors import AuthorizationError
from app.eventsourcing.domain.events import EventMetadata

logger = get_logger("app.eventsourcing.domain.bus")

#: The inner continuation a middleware calls to proceed down the pipeline.
NextHandler = Callable[["CommandContext"], Awaitable[CommandResult]]


@dataclass(slots=True)
class CommandContext:
    """The per-dispatch context threaded through the middleware pipeline.

    Attributes:
        command: the command being handled.
        metadata: provenance the caller supplied (actor, correlation id, tenant);
            the bus enriches event metadata from this.
        attributes: a scratch namespace middleware use to pass values inward
            (e.g. the auth principal, the resolved idempotency key).
    """

    command: Command
    metadata: EventMetadata = field(default_factory=EventMetadata)
    attributes: dict[str, object] = field(default_factory=dict)

    def with_metadata(self, metadata: EventMetadata) -> CommandContext:
        return CommandContext(self.command, metadata, dict(self.attributes))


class Middleware(Protocol):
    """A command-bus middleware: wrap ``handle`` around ``next``."""

    async def handle(self, ctx: CommandContext, next_: NextHandler) -> CommandResult: ...


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

#: A per-command validator: raises :class:`ValidationError` on a bad command.
CommandValidator = Callable[[Command], None]


@dataclass(slots=True)
class ValidationMiddleware:
    """Runs structural validators registered per command type.

    A command may also self-validate by implementing ``validate(self) -> None``;
    that runs first, then any registered validators for its ``command_type``.
    """

    validators: dict[str, list[CommandValidator]] = field(default_factory=dict)

    def register(self, command_type: str, validator: CommandValidator) -> None:
        self.validators.setdefault(command_type, []).append(validator)

    async def handle(self, ctx: CommandContext, next_: NextHandler) -> CommandResult:
        command = ctx.command
        self_validate = getattr(command, "validate", None)
        if callable(self_validate):
            self_validate()
        for validator in self.validators.get(command.command_type, ()):
            validator(command)
        return await next_(ctx)


# --------------------------------------------------------------------------- #
# Authorization (the auth seam)
# --------------------------------------------------------------------------- #

#: An auth policy: return ``True`` to allow, ``False`` (or raise) to deny. Given
#: the command and the supplied metadata (which carries ``actor_id``/``tenant_id``).
AuthPolicy = Callable[[Command, EventMetadata], bool]


def allow_all(_command: Command, _metadata: EventMetadata) -> bool:
    """The default permissive policy — the seam is wired to a real RBAC check later."""
    return True


@dataclass(slots=True)
class AuthorizationMiddleware:
    """The pluggable auth seam.

    Holds a single :data:`AuthPolicy` (default :func:`allow_all`). Composition can
    swap in a policy backed by :mod:`app.auth.rbac` without this layer knowing how
    authorization is decided — keeping the domain free of an auth dependency.
    """

    policy: AuthPolicy = allow_all

    async def handle(self, ctx: CommandContext, next_: NextHandler) -> CommandResult:
        allowed = self.policy(ctx.command, ctx.metadata)
        if not allowed:
            actor = ctx.metadata.actor_id or "anonymous"
            raise AuthorizationError(
                f"actor {actor!r} not authorized for {ctx.command.command_type}"
            )
        return await next_(ctx)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


class IdempotencyStore(Protocol):
    """Records handled idempotency keys -> their prior :class:`CommandResult`.

    A minimal protocol so composition can back it with Redis/Postgres later; the
    in-memory default below is enough for the write side and its tests.
    """

    async def get(self, key: str) -> CommandResult | None: ...

    async def put(self, key: str, result: CommandResult) -> None: ...


@dataclass(slots=True)
class InMemoryIdempotencyStore:
    """A dict-backed :class:`IdempotencyStore` (the reference implementation)."""

    seen: dict[str, CommandResult] = field(default_factory=dict)

    async def get(self, key: str) -> CommandResult | None:
        return self.seen.get(key)

    async def put(self, key: str, result: CommandResult) -> None:
        self.seen[key] = result


@dataclass(slots=True)
class IdempotencyMiddleware:
    """Short-circuits a command already handled under the same idempotency key.

    The key is namespaced by ``command_type`` so two different command kinds with
    the same caller-supplied key never collide. Commands with no
    :attr:`~app.eventsourcing.domain.commands.Command.idempotency_key` pass through
    untouched. On a hit, the prior result is returned with
    ``idempotent_replay=True`` so the caller can tell a replay from a fresh write
    (this is the §12.1 "re-enqueuing the same shot is a no-op" guarantee at the
    command layer — duplicate Scheduler events can never double-spend).
    """

    store: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)

    async def handle(self, ctx: CommandContext, next_: NextHandler) -> CommandResult:
        raw_key = ctx.command.idempotency_key
        if raw_key is None:
            return await next_(ctx)
        key = f"{ctx.command.command_type}:{raw_key}"
        prior = await self.store.get(key)
        if prior is not None:
            logger.info(
                "command.idempotent_replay",
                command=ctx.command.command_type,
                idempotency_key=raw_key,
            )
            return CommandResult(
                stream_id=prior.stream_id,
                events=prior.events,
                new_version=prior.new_version,
                idempotent_replay=True,
            )
        result = await next_(ctx)
        await self.store.put(key, result)
        return result


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LoggingMiddleware:
    """Structured before/after logging — no business logic, no short-circuit."""

    async def handle(self, ctx: CommandContext, next_: NextHandler) -> CommandResult:
        command_type = ctx.command.command_type
        logger.info(
            "command.dispatch",
            command=command_type,
            actor=ctx.metadata.actor_id,
            correlation_id=ctx.metadata.correlation_id,
        )
        result = await next_(ctx)
        logger.info(
            "command.handled",
            command=command_type,
            stream=result.stream_id.value,
            events=list(result.event_types),
            new_version=result.new_version,
            idempotent_replay=result.idempotent_replay,
        )
        return result


def metadata_from(mapping: Mapping[str, object]) -> EventMetadata:
    """Build :class:`EventMetadata` from a loose mapping (e.g. a request context)."""
    return EventMetadata.from_dict(mapping)


__all__ = [
    "AuthPolicy",
    "AuthorizationMiddleware",
    "CommandContext",
    "CommandValidator",
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "LoggingMiddleware",
    "Middleware",
    "NextHandler",
    "ValidationMiddleware",
    "allow_all",
    "metadata_from",
]
