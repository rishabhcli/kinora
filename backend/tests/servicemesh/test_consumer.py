"""ConsumerDispatcher: validate + route by (id,version), conversion, dead-letter.

Async dispatch driven with sync + async handlers; no infra, no network.
"""

from __future__ import annotations

import pytest

from app.servicemesh.compatibility import CompatibilityMode
from app.servicemesh.consumer import (
    ConsumerDispatcher,
    DeadLetterQueue,
    DeadLetterReason,
)
from app.servicemesh.converters import ConverterRegistry, Payload
from app.servicemesh.envelope import MessageEnvelope
from app.servicemesh.errors import SchemaNotFoundError
from app.servicemesh.registry import SchemaRegistry
from app.servicemesh.roles import ProducerRole
from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema

SCHEMA_ID = "shot.render.job"


def _registry() -> SchemaRegistry:
    reg = SchemaRegistry()
    reg.register(
        MessageSchema.from_fields(
            SCHEMA_ID,
            "1.0.0",
            [
                FieldSpec("shot_hash", FieldType.STRING),
                FieldSpec("prio", FieldType.INTEGER, required=False),
            ],
        ),
        compatibility=CompatibilityMode.NONE,  # we add a breaking rename across major
    )
    reg.register(
        MessageSchema.from_fields(
            SCHEMA_ID,
            "2.0.0",
            [
                FieldSpec("shot_hash", FieldType.STRING),
                FieldSpec("priority", FieldType.INTEGER, required=False),
                FieldSpec(
                    "mode",
                    FieldType.ENUM,
                    required=False,
                    enum_values=frozenset({"live", "card"}),
                ),
            ],
        )
    )
    return reg


def _envelope(version: str, payload: dict) -> MessageEnvelope:
    return MessageEnvelope.create(
        schema_id=SCHEMA_ID,
        schema_version=version,
        payload=payload,
        producer_role=ProducerRole.API,
    )


async def test_direct_version_hit_routes_to_handler() -> None:
    reg = _registry()
    seen: list[Payload] = []

    def handler(_env: MessageEnvelope, p: Payload) -> str:
        seen.append(p)
        return "ok"

    disp = ConsumerDispatcher(reg)
    disp.register_handler(SCHEMA_ID, "2.0.0", handler)

    outcome = await disp.dispatch(_envelope("2.0.0", {"shot_hash": "h", "priority": 3}))
    assert outcome.handled
    assert outcome.handled_version is not None and str(outcome.handled_version) == "2.0.0"
    assert outcome.converted_from is None
    assert outcome.result == "ok"
    assert seen == [{"shot_hash": "h", "priority": 3}]


async def test_async_handler_awaited() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)

    async def handler(_env: MessageEnvelope, p: Payload) -> str:
        return f"async:{p['shot_hash']}"

    disp.register_handler(SCHEMA_ID, "2.0.0", handler)
    outcome = await disp.dispatch(_envelope("2.0.0", {"shot_hash": "z"}))
    assert outcome.result == "async:z"


async def test_old_producer_upconverted_for_new_consumer() -> None:
    reg = _registry()
    conv = ConverterRegistry()
    conv.register(
        SCHEMA_ID,
        "1.0.0",
        "2.0.0",
        lambda p: {**{k: v for k, v in p.items() if k != "prio"}, "priority": p.get("prio", 0)},
    )
    seen: list[Payload] = []
    disp = ConsumerDispatcher(reg, converters=conv)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: seen.append(p))

    # Producer emits v1; consumer handles only v2 -> dispatcher converts.
    outcome = await disp.dispatch(_envelope("1.0.0", {"shot_hash": "h", "prio": 7}))
    assert outcome.handled
    assert str(outcome.converted_from) == "1.0.0"
    assert str(outcome.handled_version) == "2.0.0"
    assert seen == [{"shot_hash": "h", "priority": 7}]


async def test_unknown_schema_dead_letters() -> None:
    reg = _registry()
    dlq = DeadLetterQueue()
    disp = ConsumerDispatcher(reg, dead_letters=dlq)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)

    env = MessageEnvelope.create(schema_id="who.dis", schema_version="1.0.0", payload={})
    outcome = await disp.dispatch(env)
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.UNKNOWN_SCHEMA
    assert len(dlq) == 1


async def test_unhandled_version_no_converters_dead_letters() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)  # no converter registry populated
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)

    # v1 arrives, consumer handles v2, no migrators -> UNHANDLED_VERSION.
    outcome = await disp.dispatch(_envelope("1.0.0", {"shot_hash": "h"}))
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.UNHANDLED_VERSION


async def test_no_conversion_path_dead_letters() -> None:
    reg = _registry()
    conv = ConverterRegistry()
    # A migrator exists for the id but not one that reaches a handled version.
    conv.register(SCHEMA_ID, "2.0.0", "1.0.0", lambda p: p)
    disp = ConsumerDispatcher(reg, converters=conv)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)

    # Incoming v1, handler at v2; only a 2->1 migrator exists, so 1->2 has no path.
    outcome = await disp.dispatch(_envelope("1.0.0", {"shot_hash": "h"}))
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.NO_CONVERSION_PATH


async def test_invalid_payload_dead_letters() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)

    # Missing the required 'shot_hash'.
    outcome = await disp.dispatch(_envelope("2.0.0", {"priority": 1}))
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.PAYLOAD_INVALID


async def test_invalid_enum_value_dead_letters() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)

    outcome = await disp.dispatch(
        _envelope("2.0.0", {"shot_hash": "h", "mode": "explode"})
    )
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.PAYLOAD_INVALID


async def test_handler_error_dead_letters() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)

    def boom(_env: MessageEnvelope, _p: Payload) -> None:
        raise RuntimeError("kaboom")

    disp.register_handler(SCHEMA_ID, "2.0.0", boom)
    outcome = await disp.dispatch(_envelope("2.0.0", {"shot_hash": "h"}))
    assert not outcome.handled
    assert outcome.dead_letter is not None
    assert outcome.dead_letter.reason is DeadLetterReason.HANDLER_ERROR
    assert "kaboom" in outcome.dead_letter.detail


async def test_validation_can_be_disabled() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg, validate_payload=False)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: "routed")
    # Missing required field, but validation off -> still routes.
    outcome = await disp.dispatch(_envelope("2.0.0", {"priority": 1}))
    assert outcome.handled
    assert outcome.result == "routed"


def test_register_handler_for_unknown_schema_raises() -> None:
    reg = _registry()
    disp = ConsumerDispatcher(reg)
    with pytest.raises(SchemaNotFoundError):
        disp.register_handler("nope", "1.0.0", lambda env, p: None)


async def test_dead_letter_drain() -> None:
    reg = _registry()
    dlq = DeadLetterQueue()
    disp = ConsumerDispatcher(reg, dead_letters=dlq)
    disp.register_handler(SCHEMA_ID, "2.0.0", lambda env, p: None)
    await disp.dispatch(
        MessageEnvelope.create(schema_id="unknown", schema_version="1.0.0", payload={})
    )
    assert len(dlq) == 1
    drained = dlq.drain()
    assert len(drained) == 1
    assert len(dlq) == 0
