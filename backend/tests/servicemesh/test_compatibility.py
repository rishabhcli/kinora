"""Compatibility classification + the CI gate.

Covers each break class: add optional = compatible; add required / remove /
required->optional / narrow / enum changes / nullability = directional breaks.
"""

from __future__ import annotations

import pytest

from app.servicemesh.compatibility import (
    ChangeKind,
    CompatibilityMode,
    assert_evolution_allowed,
    check_compatibility,
    classify_changes,
)
from app.servicemesh.errors import BreakingChangeError, CompatibilityError
from app.servicemesh.schema import FieldSpec, FieldType, MessageSchema


def _schema(version: str, *fields: FieldSpec) -> MessageSchema:
    return MessageSchema.from_fields("x.msg", version, list(fields))


_BASE = (
    FieldSpec("id", FieldType.STRING),
    FieldSpec("count", FieldType.INTEGER),
)


def _kinds(old: MessageSchema, new: MessageSchema) -> set[ChangeKind]:
    return {c.kind for c in classify_changes(old, new).changes}


# -- classification: the compatible cases ----------------------------------- #
def test_identical_schema_no_changes() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.0.1", *_BASE)
    report = classify_changes(old, new)
    assert report.is_identical
    assert report.is_full


def test_add_optional_field_is_full_compatible() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.1.0", *_BASE, FieldSpec("note", FieldType.STRING, required=False))
    report = classify_changes(old, new)
    assert _kinds(old, new) == {ChangeKind.ADD_OPTIONAL_FIELD}
    assert report.is_backward and report.is_forward and report.is_full


def test_remove_optional_field_is_full_compatible() -> None:
    old = _schema("1.0.0", *_BASE, FieldSpec("note", FieldType.STRING, required=False))
    new = _schema("1.1.0", *_BASE)
    report = classify_changes(old, new)
    assert _kinds(old, new) == {ChangeKind.REMOVE_OPTIONAL_FIELD}
    assert report.is_full


# -- classification: the directional breaks --------------------------------- #
def test_add_required_field_breaks_forward_only() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.1.0", *_BASE, FieldSpec("must", FieldType.STRING))
    report = classify_changes(old, new)
    assert _kinds(old, new) == {ChangeKind.ADD_REQUIRED_FIELD}
    assert report.is_backward
    assert not report.is_forward


def test_remove_required_field_breaks_backward_only() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING))  # dropped 'count'
    report = classify_changes(old, new)
    assert _kinds(old, new) == {ChangeKind.REMOVE_REQUIRED_FIELD}
    assert not report.is_backward
    assert report.is_forward


def test_required_to_optional_breaks_forward() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema(
        "1.1.0",
        FieldSpec("id", FieldType.STRING),
        FieldSpec("count", FieldType.INTEGER, required=False),
    )
    assert _kinds(old, new) == {ChangeKind.REQUIRED_TO_OPTIONAL}
    report = classify_changes(old, new)
    assert report.is_backward and not report.is_forward


def test_optional_to_required_breaks_backward() -> None:
    old = _schema(
        "1.0.0",
        FieldSpec("id", FieldType.STRING),
        FieldSpec("count", FieldType.INTEGER, required=False),
    )
    new = _schema("1.1.0", *_BASE)
    assert _kinds(old, new) == {ChangeKind.OPTIONAL_TO_REQUIRED}
    report = classify_changes(old, new)
    assert not report.is_backward and report.is_forward


def test_widen_type_backward_only() -> None:
    old = _schema("1.0.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.INTEGER))
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.NUMBER))
    assert _kinds(old, new) == {ChangeKind.WIDEN_TYPE}
    report = classify_changes(old, new)
    assert report.is_backward and not report.is_forward


def test_narrow_type_forward_only() -> None:
    old = _schema("1.0.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.NUMBER))
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.INTEGER))
    assert _kinds(old, new) == {ChangeKind.NARROW_TYPE}
    report = classify_changes(old, new)
    assert not report.is_backward and report.is_forward


def test_incompatible_type_breaks_both() -> None:
    old = _schema("1.0.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.INTEGER))
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING), FieldSpec("count", FieldType.BOOLEAN))
    assert _kinds(old, new) == {ChangeKind.CHANGE_TYPE_INCOMPATIBLE}
    report = classify_changes(old, new)
    assert not report.is_backward and not report.is_forward


def test_add_enum_symbol_forward_only() -> None:
    old = _schema("1.0.0", FieldSpec("m", FieldType.ENUM, enum_values=frozenset({"a"})))
    new = _schema("1.1.0", FieldSpec("m", FieldType.ENUM, enum_values=frozenset({"a", "b"})))
    assert _kinds(old, new) == {ChangeKind.ADD_ENUM_SYMBOL}
    report = classify_changes(old, new)
    assert not report.is_backward and report.is_forward


def test_remove_enum_symbol_backward_only() -> None:
    old = _schema("1.0.0", FieldSpec("m", FieldType.ENUM, enum_values=frozenset({"a", "b"})))
    new = _schema("1.1.0", FieldSpec("m", FieldType.ENUM, enum_values=frozenset({"a"})))
    assert _kinds(old, new) == {ChangeKind.REMOVE_ENUM_SYMBOL}
    report = classify_changes(old, new)
    assert report.is_backward and not report.is_forward


def test_make_nullable_backward_only() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema(
        "1.1.0",
        FieldSpec("id", FieldType.STRING),
        FieldSpec("count", FieldType.INTEGER, nullable=True),
    )
    assert _kinds(old, new) == {ChangeKind.MAKE_NULLABLE}
    report = classify_changes(old, new)
    assert report.is_backward and not report.is_forward


def test_make_non_nullable_forward_only() -> None:
    old = _schema(
        "1.0.0",
        FieldSpec("id", FieldType.STRING),
        FieldSpec("count", FieldType.INTEGER, nullable=True),
    )
    new = _schema("1.1.0", *_BASE)
    assert _kinds(old, new) == {ChangeKind.MAKE_NON_NULLABLE}
    report = classify_changes(old, new)
    assert not report.is_backward and report.is_forward


def test_classify_rejects_mismatched_ids() -> None:
    a = MessageSchema.from_fields("a", "1.0.0", [FieldSpec("x", FieldType.STRING)])
    b = MessageSchema.from_fields("b", "1.0.0", [FieldSpec("x", FieldType.STRING)])
    with pytest.raises(CompatibilityError):
        classify_changes(a, b)


# -- check_compatibility predicate ------------------------------------------ #
def test_check_compatibility_predicate() -> None:
    old = _schema("1.0.0", *_BASE)
    add_required = _schema("1.1.0", *_BASE, FieldSpec("must", FieldType.STRING))
    assert check_compatibility(old, add_required, CompatibilityMode.BACKWARD)
    assert not check_compatibility(old, add_required, CompatibilityMode.FORWARD)
    assert not check_compatibility(old, add_required, CompatibilityMode.FULL)
    assert check_compatibility(old, add_required, CompatibilityMode.NONE)


# -- the CI gate ------------------------------------------------------------ #
def test_gate_rejects_breaking_on_stable_channel() -> None:
    old = _schema("1.0.0", *_BASE)
    # Removing a required field breaks BACKWARD compatibility.
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING))
    with pytest.raises(BreakingChangeError) as exc:
        assert_evolution_allowed(old, new, CompatibilityMode.BACKWARD)
    assert "stable channel" in str(exc.value)
    assert "remove_required_field" in str(exc.value)


def test_gate_allows_compatible_on_stable_channel() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.1.0", *_BASE, FieldSpec("note", FieldType.STRING, required=False))
    report = assert_evolution_allowed(old, new, CompatibilityMode.BACKWARD)
    assert report.is_backward


def test_gate_allows_breaking_on_major_bump() -> None:
    old = _schema("1.0.0", *_BASE)
    # A breaking removal, but declared via a MAJOR bump -> allowed.
    new = _schema("2.0.0", FieldSpec("id", FieldType.STRING))
    report = assert_evolution_allowed(old, new, CompatibilityMode.FULL)
    assert ChangeKind.REMOVE_REQUIRED_FIELD in {c.kind for c in report.changes}


def test_gate_tolerates_breaking_pre_1_0() -> None:
    old = _schema("0.3.0", *_BASE)
    new = _schema("0.4.0", FieldSpec("id", FieldType.STRING))  # breaking removal
    # Pre-1.0 with stable_only honours "minor may break".
    assert_evolution_allowed(old, new, CompatibilityMode.BACKWARD, stable_only=True)
    # But with stable_only disabled, the gate enforces regardless of version.
    with pytest.raises(BreakingChangeError):
        assert_evolution_allowed(old, new, CompatibilityMode.BACKWARD, stable_only=False)


def test_gate_full_mode_requires_both_directions() -> None:
    old = _schema("1.0.0", *_BASE)
    add_required = _schema("1.1.0", *_BASE, FieldSpec("must", FieldType.STRING))
    # backward-only -> fails FULL but passes BACKWARD.
    assert_evolution_allowed(old, add_required, CompatibilityMode.BACKWARD)
    with pytest.raises(BreakingChangeError):
        assert_evolution_allowed(old, add_required, CompatibilityMode.FULL)


def test_breaking_changes_lists_offenders() -> None:
    old = _schema("1.0.0", *_BASE)
    new = _schema("1.1.0", FieldSpec("id", FieldType.STRING))  # removed count
    report = classify_changes(old, new)
    offenders = report.breaking_changes(CompatibilityMode.BACKWARD)
    assert len(offenders) == 1
    assert offenders[0].kind is ChangeKind.REMOVE_REQUIRED_FIELD
    # FORWARD is satisfied so there are no forward offenders.
    assert report.breaking_changes(CompatibilityMode.FORWARD) == ()
