"""MessageSchema: structural description, canonical form, content hashing."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema, widens_to


def _schema(*fields: FieldSpec) -> MessageSchema:
    return MessageSchema.from_fields("x.msg", "1.0.0", list(fields))


def test_content_hash_is_deterministic() -> None:
    a = _schema(FieldSpec("a", FieldType.STRING), FieldSpec("b", FieldType.INTEGER))
    b = _schema(FieldSpec("a", FieldType.STRING), FieldSpec("b", FieldType.INTEGER))
    assert a.content_hash() == b.content_hash()
    assert a.content_hash().startswith("sha256:")


def test_content_hash_invariant_under_field_order() -> None:
    a = _schema(FieldSpec("a", FieldType.STRING), FieldSpec("b", FieldType.INTEGER))
    b = _schema(FieldSpec("b", FieldType.INTEGER), FieldSpec("a", FieldType.STRING))
    assert a.content_hash() == b.content_hash()


def test_content_hash_ignores_title_and_description() -> None:
    a = MessageSchema.from_fields(
        "x.msg", "1.0.0", [FieldSpec("a", FieldType.STRING, description="one")], title="A"
    )
    b = MessageSchema.from_fields(
        "x.msg", "1.0.0", [FieldSpec("a", FieldType.STRING, description="two")], title="B"
    )
    assert a.content_hash() == b.content_hash()


def test_content_hash_changes_with_shape() -> None:
    a = _schema(FieldSpec("a", FieldType.STRING))
    b = _schema(FieldSpec("a", FieldType.STRING, required=False))
    c = _schema(FieldSpec("a", FieldType.INTEGER))
    assert a.content_hash() != b.content_hash()
    assert a.content_hash() != c.content_hash()


def test_widening_lattice() -> None:
    assert widens_to(FieldType.INTEGER, FieldType.NUMBER)
    assert widens_to(FieldType.INTEGER, FieldType.ANY)
    assert widens_to(FieldType.ENUM, FieldType.STRING)
    assert not widens_to(FieldType.NUMBER, FieldType.INTEGER)
    assert not widens_to(FieldType.STRING, FieldType.INTEGER)


def test_from_model_classifies_annotations() -> None:
    class Mode(StrEnum):
        LIVE = "live"
        CARD = "card"

    class Job(BaseModel):
        shot_hash: str
        priority: int
        budget: float | None = None
        mode: Mode
        tags: list[str] = []

    schema = MessageSchema.from_model("job", "1.0.0", Job)
    by_name = schema.fields_by_name
    assert by_name["shot_hash"].type is FieldType.STRING
    assert by_name["shot_hash"].required
    assert by_name["priority"].type is FieldType.INTEGER
    assert by_name["budget"].type is FieldType.NUMBER
    assert by_name["budget"].nullable
    assert not by_name["budget"].required
    assert by_name["mode"].type is FieldType.ENUM
    assert by_name["mode"].enum_values == frozenset({"live", "card"})
    assert by_name["tags"].type is FieldType.ARRAY
    assert by_name["tags"].item_type is FieldType.STRING


def test_fields_by_name_and_key() -> None:
    s = _schema(FieldSpec("a", FieldType.STRING))
    assert "a" in s.fields_by_name
    assert s.key()[0] == "x.msg"
