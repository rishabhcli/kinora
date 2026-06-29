"""Unit tests for the command-bus middleware in isolation: validation, the auth
seam, idempotency, and logging. Each is fed a context + a stub ``next``."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.eventsourcing.domain.commands import Command, CommandResult
from app.eventsourcing.domain.errors import AuthorizationError, ValidationError
from app.eventsourcing.domain.events import EventMetadata
from app.eventsourcing.domain.identifiers import StreamId
from app.eventsourcing.domain.middleware import (
    AuthorizationMiddleware,
    CommandContext,
    IdempotencyMiddleware,
    InMemoryIdempotencyStore,
    LoggingMiddleware,
    ValidationMiddleware,
)


@dataclass(frozen=True, slots=True)
class _Cmd(Command):
    sid: str = "s1"
    key: str | None = None

    def target_stream(self) -> StreamId:
        return StreamId.session(self.sid)

    @property
    def idempotency_key(self) -> str | None:
        return self.key


def _ctx(cmd: Command, **meta: object) -> CommandContext:
    return CommandContext(command=cmd, metadata=EventMetadata(**meta))  # type: ignore[arg-type]


async def _ok_next(ctx: CommandContext) -> CommandResult:
    return CommandResult(stream_id=ctx.command.target_stream(), new_version=1)


# -- validation -------------------------------------------------------------- #


async def test_validation_runs_registered_validator() -> None:
    mw = ValidationMiddleware()
    called: list[Command] = []

    def validator(cmd: Command) -> None:
        called.append(cmd)

    mw.register(_Cmd.command_type, validator)
    await mw.handle(_ctx(_Cmd()), _ok_next)
    assert len(called) == 1


async def test_validation_propagates_error() -> None:
    mw = ValidationMiddleware()

    def validator(_cmd: Command) -> None:
        raise ValidationError("bad")

    mw.register(_Cmd.command_type, validator)
    with pytest.raises(ValidationError):
        await mw.handle(_ctx(_Cmd()), _ok_next)


async def test_validation_runs_self_validate_method() -> None:
    @dataclass(frozen=True, slots=True)
    class _SelfValidating(Command):
        def target_stream(self) -> StreamId:
            return StreamId.session("s")

        def validate(self) -> None:
            raise ValidationError("self")

    mw = ValidationMiddleware()
    with pytest.raises(ValidationError, match="self"):
        await mw.handle(_ctx(_SelfValidating()), _ok_next)


# -- auth -------------------------------------------------------------------- #


async def test_auth_allows_by_default() -> None:
    mw = AuthorizationMiddleware()
    result = await mw.handle(_ctx(_Cmd()), _ok_next)
    assert result.new_version == 1


async def test_auth_denies_via_policy() -> None:
    mw = AuthorizationMiddleware(policy=lambda _c, _m: False)
    with pytest.raises(AuthorizationError):
        await mw.handle(_ctx(_Cmd(), actor_id="mallory"), _ok_next)


async def test_auth_policy_sees_command_and_metadata() -> None:
    seen: list[tuple[str, str | None]] = []

    def policy(cmd: Command, meta: EventMetadata) -> bool:
        seen.append((cmd.command_type, meta.actor_id))
        return True

    mw = AuthorizationMiddleware(policy=policy)
    await mw.handle(_ctx(_Cmd(), actor_id="u1"), _ok_next)
    assert seen == [("_Cmd", "u1")]


# -- idempotency ------------------------------------------------------------- #


async def test_idempotency_passes_through_when_no_key() -> None:
    mw = IdempotencyMiddleware()
    calls = 0

    async def counting_next(ctx: CommandContext) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(stream_id=ctx.command.target_stream(), new_version=calls)

    await mw.handle(_ctx(_Cmd(key=None)), counting_next)
    await mw.handle(_ctx(_Cmd(key=None)), counting_next)
    assert calls == 2  # no dedupe without a key


async def test_idempotency_short_circuits_on_replay() -> None:
    store = InMemoryIdempotencyStore()
    mw = IdempotencyMiddleware(store=store)
    calls = 0

    async def counting_next(ctx: CommandContext) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(stream_id=ctx.command.target_stream(), new_version=7)

    first = await mw.handle(_ctx(_Cmd(key="k1")), counting_next)
    second = await mw.handle(_ctx(_Cmd(key="k1")), counting_next)
    assert calls == 1
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.new_version == 7


async def test_idempotency_namespaces_by_command_type() -> None:
    @dataclass(frozen=True, slots=True)
    class _Other(Command):
        key: str = "k1"

        def target_stream(self) -> StreamId:
            return StreamId.session("s")

        @property
        def idempotency_key(self) -> str | None:
            return self.key

    store = InMemoryIdempotencyStore()
    mw = IdempotencyMiddleware(store=store)
    calls = 0

    async def counting_next(ctx: CommandContext) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(stream_id=ctx.command.target_stream())

    # Same key "k1" but different command types do not collide.
    await mw.handle(_ctx(_Cmd(key="k1")), counting_next)
    await mw.handle(_ctx(_Other()), counting_next)
    assert calls == 2


# -- logging ----------------------------------------------------------------- #


async def test_logging_is_transparent() -> None:
    mw = LoggingMiddleware()
    result = await mw.handle(_ctx(_Cmd()), _ok_next)
    assert result.new_version == 1
