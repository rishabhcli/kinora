"""SchemaRegistry: registration, content hash, idempotency, evolution gate, lookup."""

from __future__ import annotations

import pytest

from app.servicemesh.compatibility import CompatibilityMode
from app.servicemesh.errors import (
    BreakingChangeError,
    SchemaAlreadyRegisteredError,
    SchemaHashMismatchError,
    SchemaNotFoundError,
)
from app.servicemesh.registry import RegisteredSchema, SchemaRegistry
from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema
from app.servicemesh.versioning import SemVer


def _schema(version: str, *fields: FieldSpec) -> MessageSchema:
    return MessageSchema.from_fields("x.msg", version, list(fields))


_BASE = (FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.INTEGER))


def test_register_and_get() -> None:
    reg = SchemaRegistry()
    s = _schema("1.0.0", *_BASE)
    entry = reg.register(s)
    assert isinstance(entry, RegisteredSchema)
    assert entry.content_hash == s.content_hash()
    assert reg.get("x.msg", "1.0.0").schema is s
    assert reg.has("x.msg")
    assert reg.has("x.msg", "1.0.0")
    assert not reg.has("x.msg", "9.9.9")


def test_get_unknown_raises() -> None:
    reg = SchemaRegistry()
    with pytest.raises(SchemaNotFoundError):
        reg.get("nope", "1.0.0")
    with pytest.raises(SchemaNotFoundError):
        reg.latest("nope")
    with pytest.raises(SchemaNotFoundError):
        reg.versions("nope")


def test_idempotent_reregistration_same_shape() -> None:
    reg = SchemaRegistry()
    a = reg.register(_schema("1.0.0", *_BASE))
    b = reg.register(_schema("1.0.0", *_BASE))  # identical shape, same version
    assert a is b  # returns the existing entry, no error


def test_reregister_different_shape_same_version_raises() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE))
    with pytest.raises(SchemaAlreadyRegisteredError):
        reg.register(_schema("1.0.0", FieldSpec("id", FieldType.STRING)))  # different shape


def test_evolution_gate_blocks_breaking_on_stable_channel() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE), compatibility=CompatibilityMode.BACKWARD)
    # New version drops a required field -> backward break -> rejected.
    with pytest.raises(BreakingChangeError):
        reg.register(_schema("1.1.0", FieldSpec("id", FieldType.STRING)))


def test_evolution_gate_allows_compatible() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE))
    entry = reg.register(
        _schema("1.1.0", *_BASE, FieldSpec("note", FieldType.STRING, required=False))
    )
    assert entry.version == SemVer.parse("1.1.0")
    assert reg.versions("x.msg") == [SemVer.parse("1.0.0"), SemVer.parse("1.1.0")]
    assert reg.latest("x.msg").version == SemVer.parse("1.1.0")


def test_evolution_gate_allows_major_bump_break() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE), compatibility=CompatibilityMode.FULL)
    # Breaking, but MAJOR bump declares it intentional.
    entry = reg.register(_schema("2.0.0", FieldSpec("id", FieldType.STRING)))
    assert entry.version == SemVer.parse("2.0.0")


def test_channel_compatibility_cannot_silently_change() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE), compatibility=CompatibilityMode.BACKWARD)
    with pytest.raises(SchemaAlreadyRegisteredError):
        reg.register(_schema("1.1.0", *_BASE), compatibility=CompatibilityMode.FULL)


def test_declare_channel_redeclare_mismatch_raises() -> None:
    reg = SchemaRegistry()
    reg.declare_channel("c", CompatibilityMode.BACKWARD)
    # Re-declaring same mode is fine.
    reg.declare_channel("c", CompatibilityMode.BACKWARD)
    with pytest.raises(SchemaAlreadyRegisteredError):
        reg.declare_channel("c", CompatibilityMode.FULL)


def test_gate_compares_against_immediate_predecessor() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE))
    reg.register(_schema("1.1.0", *_BASE, FieldSpec("a", FieldType.STRING, required=False)))
    # 1.2.0 only adds another optional field vs 1.1.0 -> still compatible.
    reg.register(
        _schema(
            "1.2.0",
            *_BASE,
            FieldSpec("a", FieldType.STRING, required=False),
            FieldSpec("b", FieldType.STRING, required=False),
        )
    )
    assert [str(v) for v in reg.versions("x.msg")] == ["1.0.0", "1.1.0", "1.2.0"]


def test_verify_hashes_passes_on_clean_registry() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE))
    reg.verify_hashes()  # no raise


def test_verify_hashes_detects_tampered_entry() -> None:
    reg = SchemaRegistry()
    reg.register(_schema("1.0.0", *_BASE))
    # Corrupt a stored entry's recorded hash to simulate drift.
    channel = reg.channel("x.msg")
    version = next(iter(channel.versions))
    channel.versions[version] = RegisteredSchema(
        schema=channel.versions[version].schema, content_hash="sha256:bogus"
    )
    with pytest.raises(SchemaHashMismatchError):
        reg.verify_hashes()


def test_schema_ids_sorted() -> None:
    reg = SchemaRegistry()
    reg.register(MessageSchema.from_fields("z", "1.0.0", [FieldSpec("a", FieldType.STRING)]))
    reg.register(MessageSchema.from_fields("a", "1.0.0", [FieldSpec("a", FieldType.STRING)]))
    assert reg.schema_ids() == ["a", "z"]
