"""Typed consumer dispatch — validate + route by (schema id, version), dead-letter.

A consumer registers a handler for a ``(schema_id, target_version)`` it natively
understands. When an envelope arrives, the dispatcher:

1. parses/validates the envelope frame,
2. looks the schema up in the registry (unknown id -> dead-letter),
3. if the envelope's version differs from a handled one, asks the
   :class:`~app.servicemesh.converters.ConverterRegistry` to up/down-convert the
   payload to a version it *does* handle (graceful negotiation),
4. validates the (converted) payload against the target schema's structure,
5. routes to the handler.

Anything unhandlable — an unknown schema id, a version with no handler *and* no
conversion path, a payload that fails structural validation, or a handler that
raises — lands in a :class:`DeadLetterQueue` with the reason, rather than crashing
the worker loop. This is the §12 "dead-lettered render queue" discipline applied to
the *contract* layer.

Handlers are plain callables (sync or async); :meth:`dispatch` is async and awaits
coroutine handlers so it drops cleanly into the async worker/api roles, but the unit
tests drive it with sync handlers and no event-loop infra.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from app.servicemesh.converters import ConverterRegistry, Payload
from app.servicemesh.envelope import MessageEnvelope
from app.servicemesh.errors import (
    NoConversionPathError,
)
from app.servicemesh.registry import SchemaRegistry
from app.servicemesh.schema import FieldType, MessageSchema
from app.servicemesh.versioning import SemVer

__all__ = [
    "DeadLetterReason",
    "DeadLetter",
    "DeadLetterQueue",
    "DispatchOutcome",
    "ConsumerDispatcher",
    "HandlerResult",
]

log = structlog.get_logger("app.servicemesh.consumer")

HandlerResult = Any
Handler = Callable[[MessageEnvelope, Payload], HandlerResult | Awaitable[HandlerResult]]


class DeadLetterReason(StrEnum):
    """Why an envelope could not be handled."""

    UNKNOWN_SCHEMA = "unknown_schema"
    UNHANDLED_VERSION = "unhandled_version"
    NO_CONVERSION_PATH = "no_conversion_path"
    PAYLOAD_INVALID = "payload_invalid"
    HANDLER_ERROR = "handler_error"
    DECODE_ERROR = "decode_error"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A parked, unhandlable message + the reason it failed."""

    reason: DeadLetterReason
    detail: str
    schema_id: str | None = None
    version: str | None = None
    message_id: str | None = None
    envelope: MessageEnvelope | None = None


class DeadLetterQueue:
    """An in-memory dead-letter sink (the seam to a durable DLQ in production)."""

    def __init__(self) -> None:
        self._items: list[DeadLetter] = []
        self._lock = threading.Lock()

    def put(self, entry: DeadLetter) -> None:
        with self._lock:
            self._items.append(entry)
        log.warning(
            "servicemesh.deadletter",
            reason=entry.reason.value,
            schema_id=entry.schema_id,
            version=entry.version,
            message_id=entry.message_id,
            detail=entry.detail,
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def items(self) -> list[DeadLetter]:
        with self._lock:
            return list(self._items)

    def drain(self) -> list[DeadLetter]:
        """Remove and return all parked entries."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            return out


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """The result of dispatching one envelope."""

    handled: bool
    schema_id: str
    handled_version: SemVer | None = None
    converted_from: SemVer | None = None
    result: HandlerResult = None
    dead_letter: DeadLetter | None = None


@dataclass(slots=True)
class _HandlerEntry:
    schema_id: str
    version: SemVer
    schema: MessageSchema
    handler: Handler


def _validate_payload(schema: MessageSchema, payload: Payload) -> str | None:
    """Lightweight structural validation; returns an error string or ``None``.

    Checks the *contract-relevant* invariants the registry can express: required
    fields present and non-null (unless nullable), and closed enum domains. It is
    intentionally permissive about extra keys — a forward-compatible consumer may
    receive fields it does not model.
    """
    by_name = payload
    for spec in schema.fields:
        present = spec.name in by_name
        if spec.required and not present:
            return f"missing required field {spec.name!r}"
        if not present:
            continue
        value = by_name[spec.name]
        if value is None:
            if not spec.nullable and spec.required:
                return f"field {spec.name!r} is null but not nullable"
            continue
        if (
            spec.type is FieldType.ENUM
            and spec.enum_values
            and str(value) not in spec.enum_values
        ):
            return (
                f"field {spec.name!r} value {value!r} not in enum domain "
                f"{sorted(spec.enum_values)}"
            )
    return None


class ConsumerDispatcher:
    """Validates + routes envelopes by (schema id, version), with a dead-letter."""

    def __init__(
        self,
        registry: SchemaRegistry,
        converters: ConverterRegistry | None = None,
        dead_letters: DeadLetterQueue | None = None,
        *,
        validate_payload: bool = True,
    ) -> None:
        self._registry = registry
        # NB: use explicit ``is None`` — a DeadLetterQueue defines __len__, so an
        # empty queue is falsy and ``x or default`` would silently discard it.
        self._converters = converters if converters is not None else ConverterRegistry()
        self._dlq = dead_letters if dead_letters is not None else DeadLetterQueue()
        self._validate = validate_payload
        # schema_id -> {version: handler entry}
        self._handlers: dict[str, dict[SemVer, _HandlerEntry]] = {}
        self._lock = threading.RLock()

    @property
    def dead_letters(self) -> DeadLetterQueue:
        return self._dlq

    # -- registration ------------------------------------------------------- #
    def register_handler(
        self, schema_id: str, version: SemVer | str, handler: Handler
    ) -> None:
        """Bind a handler to a ``(schema_id, version)`` the consumer understands.

        The schema must already be in the registry — the handler is for a *known*
        shape, and dispatch validates against it.
        """
        ver = SemVer.coerce(version)
        entry = self._registry.get(schema_id, ver)  # raises if unknown
        with self._lock:
            self._handlers.setdefault(schema_id, {})[ver] = _HandlerEntry(
                schema_id=schema_id,
                version=ver,
                schema=entry.schema,
                handler=handler,
            )

    def handled_versions(self, schema_id: str) -> list[SemVer]:
        """The versions this consumer has a handler for (ascending)."""
        with self._lock:
            return sorted(self._handlers.get(schema_id, {}))

    # -- dispatch ----------------------------------------------------------- #
    async def dispatch(self, envelope: MessageEnvelope) -> DispatchOutcome:
        """Validate, (convert,) and route ``envelope`` — or dead-letter it."""
        schema_id = envelope.schema_id
        incoming = envelope.version

        with self._lock:
            handlers = dict(self._handlers.get(schema_id, {}))

        if not handlers:
            return self._dead(
                envelope,
                DeadLetterReason.UNKNOWN_SCHEMA,
                f"no handler registered for schema id {schema_id!r}",
            )

        # 1. Direct hit on the exact version.
        target_version, payload, converted_from = self._resolve_version(
            schema_id, incoming, handlers, envelope.payload
        )
        if target_version is None:
            # No exact handler for the version, and no migrator chain reaches any
            # version we do handle. If the consumer has handlers at all (it does, we
            # checked), the precise cause is "the version is unreachable" — surface
            # NO_CONVERSION_PATH when migrators are registered for this id at all,
            # else UNHANDLED_VERSION (nothing was ever wired to bridge versions).
            reason = (
                DeadLetterReason.NO_CONVERSION_PATH
                if self._converters.has_any(schema_id)
                else DeadLetterReason.UNHANDLED_VERSION
            )
            return self._dead(
                envelope,
                reason,
                f"version {incoming} of {schema_id!r} has no handler and no "
                f"conversion path to {sorted(handlers)}",
            )

        entry = handlers[target_version]

        # 2. Structural validation against the target schema.
        if self._validate:
            error = _validate_payload(entry.schema, payload)
            if error is not None:
                return self._dead(
                    envelope,
                    DeadLetterReason.PAYLOAD_INVALID,
                    f"{schema_id}@{target_version}: {error}",
                )

        # 3. Route.
        try:
            result = entry.handler(envelope, payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - isolate handler faults
            return self._dead(
                envelope,
                DeadLetterReason.HANDLER_ERROR,
                f"handler for {schema_id}@{target_version} raised: {exc}",
            )

        return DispatchOutcome(
            handled=True,
            schema_id=schema_id,
            handled_version=target_version,
            converted_from=converted_from,
            result=result,
        )

    def _resolve_version(
        self,
        schema_id: str,
        incoming: SemVer,
        handlers: dict[SemVer, _HandlerEntry],
        payload: Payload,
    ) -> tuple[SemVer | None, Payload, SemVer | None]:
        """Pick a target version + (possibly converted) payload.

        Prefers an exact handler. Otherwise converts toward the *closest* handled
        version with an available chain — nearest by version distance, preferring an
        upgrade target on ties (a newer consumer reading an older producer).
        """
        if incoming in handlers:
            return incoming, payload, None

        # Candidate handled versions reachable via a conversion chain, ranked by
        # version distance then direction (upgrade preferred).
        candidates = sorted(
            handlers,
            key=lambda v: (_distance(incoming, v), 0 if v > incoming else 1),
        )
        for target in candidates:
            try:
                converted = self._converters.convert(
                    schema_id, payload, incoming, target
                )
            except NoConversionPathError:
                continue
            return target, converted, incoming
        return None, payload, None

    # -- helpers ------------------------------------------------------------ #
    def _dead(
        self, envelope: MessageEnvelope, reason: DeadLetterReason, detail: str
    ) -> DispatchOutcome:
        entry = DeadLetter(
            reason=reason,
            detail=detail,
            schema_id=envelope.schema_id,
            version=envelope.schema_version,
            message_id=envelope.message_id,
            envelope=envelope,
        )
        self._dlq.put(entry)
        return DispatchOutcome(
            handled=False, schema_id=envelope.schema_id, dead_letter=entry
        )


def _distance(a: SemVer, b: SemVer) -> tuple[int, int, int]:
    """An ordering key approximating "how far apart" two versions are."""
    return (
        abs(a.major - b.major),
        abs(a.minor - b.minor),
        abs(a.patch - b.patch),
    )
