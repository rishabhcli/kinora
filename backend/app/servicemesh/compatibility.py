"""Schema-evolution compatibility classification + the CI gate.

Given an *old* and a *new* :class:`~app.servicemesh.schema.MessageSchema` for the
same ``schema_id``, this module classifies every structural delta and decides
whether the evolution is allowed on a channel held to a stability contract.

The vocabulary is the standard schema-registry one:

* **BACKWARD** compatible — a *new consumer* can read messages produced by an *old
  producer*. Achieved by: adding an *optional* field, removing a field the consumer
  no longer reads, relaxing required->optional, widening a type, growing an enum's
  domain (a producer can never emit a symbol the consumer rejects... wait — see
  below), making a field nullable.
* **FORWARD** compatible — an *old consumer* can read messages produced by a *new
  producer*. The mirror image.
* **FULL** = BACKWARD ∧ FORWARD.

Direction matters per change, so we classify each delta independently and fold:

| change                        | backward | forward |
|-------------------------------|----------|---------|
| add optional field            | yes      | yes     |
| add required field            | yes*    | no      |
| remove optional field         | yes      | yes     |
| remove required field         | no       | yes     |
| required -> optional          | yes      | no      |
| optional -> required          | no       | yes     |
| widen type (int->number)      | yes      | no      |
| narrow type (number->int)     | no       | yes     |
| add enum symbol               | no       | yes     |
| remove enum symbol            | yes      | no      |
| make nullable                 | yes      | no      |
| make non-nullable             | no       | yes     |

* "add required field" is backward-safe for a *consumer* (it just sees an extra
required field every new message has) but forward-*un*safe (an old producer omits
it). We follow the Confluent convention: **adding a field with no default is a
forward break**, and we treat a *required* added field as backward-safe only when
the message is produced by the new code. The CI gate keys off the channel mode.

The CI gate, :func:`assert_evolution_allowed`, raises
:class:`~app.servicemesh.errors.BreakingChangeError` when the classified change is
incompatible with the channel's declared mode *and* the channel is stable — exactly
the check a contract test runs in CI before a schema change merges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.servicemesh.errors import BreakingChangeError, CompatibilityError
from app.servicemesh.schema import FieldSpec, MessageSchema, widens_to

__all__ = [
    "CompatibilityMode",
    "ChangeKind",
    "SchemaChange",
    "CompatibilityReport",
    "classify_changes",
    "check_compatibility",
    "assert_evolution_allowed",
]


class CompatibilityMode(StrEnum):
    """The stability contract a channel is held to."""

    NONE = "none"  # anything goes (a dev/experimental channel)
    BACKWARD = "backward"
    FORWARD = "forward"
    FULL = "full"


class ChangeKind(StrEnum):
    """The classified delta between two schema versions, one per field event."""

    ADD_OPTIONAL_FIELD = "add_optional_field"
    ADD_REQUIRED_FIELD = "add_required_field"
    REMOVE_OPTIONAL_FIELD = "remove_optional_field"
    REMOVE_REQUIRED_FIELD = "remove_required_field"
    REQUIRED_TO_OPTIONAL = "required_to_optional"
    OPTIONAL_TO_REQUIRED = "optional_to_required"
    WIDEN_TYPE = "widen_type"
    NARROW_TYPE = "narrow_type"
    CHANGE_TYPE_INCOMPATIBLE = "change_type_incompatible"
    ADD_ENUM_SYMBOL = "add_enum_symbol"
    REMOVE_ENUM_SYMBOL = "remove_enum_symbol"
    MAKE_NULLABLE = "make_nullable"
    MAKE_NON_NULLABLE = "make_non_nullable"


# Per-change compatibility table (backward, forward). The fold over a delta set is
# the conjunction: an evolution is backward-compatible iff *every* change is.
_TABLE: dict[ChangeKind, tuple[bool, bool]] = {
    ChangeKind.ADD_OPTIONAL_FIELD: (True, True),
    ChangeKind.ADD_REQUIRED_FIELD: (True, False),
    ChangeKind.REMOVE_OPTIONAL_FIELD: (True, True),
    ChangeKind.REMOVE_REQUIRED_FIELD: (False, True),
    ChangeKind.REQUIRED_TO_OPTIONAL: (True, False),
    ChangeKind.OPTIONAL_TO_REQUIRED: (False, True),
    ChangeKind.WIDEN_TYPE: (True, False),
    ChangeKind.NARROW_TYPE: (False, True),
    ChangeKind.CHANGE_TYPE_INCOMPATIBLE: (False, False),
    ChangeKind.ADD_ENUM_SYMBOL: (False, True),
    ChangeKind.REMOVE_ENUM_SYMBOL: (True, False),
    ChangeKind.MAKE_NULLABLE: (True, False),
    ChangeKind.MAKE_NON_NULLABLE: (False, True),
}


@dataclass(frozen=True, slots=True)
class SchemaChange:
    """One classified delta on a single field."""

    kind: ChangeKind
    field_name: str
    detail: str = ""

    @property
    def is_backward(self) -> bool:
        return _TABLE[self.kind][0]

    @property
    def is_forward(self) -> bool:
        return _TABLE[self.kind][1]


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """The result of comparing two schema versions."""

    schema_id: str
    old_version: str
    new_version: str
    changes: tuple[SchemaChange, ...] = field(default_factory=tuple)

    @property
    def is_backward(self) -> bool:
        return all(c.is_backward for c in self.changes)

    @property
    def is_forward(self) -> bool:
        return all(c.is_forward for c in self.changes)

    @property
    def is_full(self) -> bool:
        return self.is_backward and self.is_forward

    @property
    def is_identical(self) -> bool:
        return not self.changes

    def satisfies(self, mode: CompatibilityMode) -> bool:
        """Whether this evolution meets the requested compatibility contract."""
        if mode == CompatibilityMode.NONE:
            return True
        if mode == CompatibilityMode.BACKWARD:
            return self.is_backward
        if mode == CompatibilityMode.FORWARD:
            return self.is_forward
        return self.is_full

    def breaking_changes(self, mode: CompatibilityMode) -> tuple[SchemaChange, ...]:
        """The subset of changes that violate ``mode`` (empty if satisfied)."""
        if mode == CompatibilityMode.NONE:
            return ()
        if mode == CompatibilityMode.BACKWARD:
            return tuple(c for c in self.changes if not c.is_backward)
        if mode == CompatibilityMode.FORWARD:
            return tuple(c for c in self.changes if not c.is_forward)
        return tuple(c for c in self.changes if not (c.is_backward and c.is_forward))


def _classify_field_change(old: FieldSpec, new: FieldSpec) -> list[SchemaChange]:
    """Classify the deltas on a field present in both schemas."""
    changes: list[SchemaChange] = []
    name = new.name

    # required <-> optional
    if old.required and not new.required:
        changes.append(SchemaChange(ChangeKind.REQUIRED_TO_OPTIONAL, name))
    elif not old.required and new.required:
        changes.append(SchemaChange(ChangeKind.OPTIONAL_TO_REQUIRED, name))

    # nullability
    if not old.nullable and new.nullable:
        changes.append(SchemaChange(ChangeKind.MAKE_NULLABLE, name))
    elif old.nullable and not new.nullable:
        changes.append(SchemaChange(ChangeKind.MAKE_NON_NULLABLE, name))

    # type changes
    if old.type != new.type:
        if widens_to(old.type, new.type):
            changes.append(
                SchemaChange(
                    ChangeKind.WIDEN_TYPE, name, f"{old.type.value}->{new.type.value}"
                )
            )
        elif widens_to(new.type, old.type):
            changes.append(
                SchemaChange(
                    ChangeKind.NARROW_TYPE, name, f"{old.type.value}->{new.type.value}"
                )
            )
        else:
            changes.append(
                SchemaChange(
                    ChangeKind.CHANGE_TYPE_INCOMPATIBLE,
                    name,
                    f"{old.type.value}->{new.type.value}",
                )
            )

    # enum domain (only meaningful when both are enums)
    if old.type == new.type and old.enum_values != new.enum_values:
        added = new.enum_values - old.enum_values
        removed = old.enum_values - new.enum_values
        if added:
            changes.append(
                SchemaChange(ChangeKind.ADD_ENUM_SYMBOL, name, ",".join(sorted(added)))
            )
        if removed:
            changes.append(
                SchemaChange(
                    ChangeKind.REMOVE_ENUM_SYMBOL, name, ",".join(sorted(removed))
                )
            )

    return changes


def classify_changes(old: MessageSchema, new: MessageSchema) -> CompatibilityReport:
    """Diff two versions of the same schema into a classified change set."""
    if old.schema_id != new.schema_id:
        raise CompatibilityError(
            f"cannot compare different schemas: {old.schema_id!r} vs {new.schema_id!r}"
        )

    old_fields = old.fields_by_name
    new_fields = new.fields_by_name
    changes: list[SchemaChange] = []

    # Added fields.
    for name in new_fields.keys() - old_fields.keys():
        spec = new_fields[name]
        kind = (
            ChangeKind.ADD_REQUIRED_FIELD
            if spec.required and not spec.nullable
            else ChangeKind.ADD_OPTIONAL_FIELD
        )
        changes.append(SchemaChange(kind, name))

    # Removed fields.
    for name in old_fields.keys() - new_fields.keys():
        spec = old_fields[name]
        kind = (
            ChangeKind.REMOVE_REQUIRED_FIELD
            if spec.required and not spec.nullable
            else ChangeKind.REMOVE_OPTIONAL_FIELD
        )
        changes.append(SchemaChange(kind, name))

    # Modified fields (present in both).
    for name in old_fields.keys() & new_fields.keys():
        changes.extend(_classify_field_change(old_fields[name], new_fields[name]))

    # Deterministic ordering for stable reports/tests.
    changes.sort(key=lambda c: (c.field_name, c.kind.value))
    return CompatibilityReport(
        schema_id=new.schema_id,
        old_version=str(old.version),
        new_version=str(new.version),
        changes=tuple(changes),
    )


def check_compatibility(
    old: MessageSchema, new: MessageSchema, mode: CompatibilityMode
) -> bool:
    """Whether ``old -> new`` satisfies ``mode`` (does not raise; pure predicate).

    The non-raising counterpart to :func:`assert_evolution_allowed`, ignoring the
    SemVer stable-only / MAJOR-bump exemptions — a strict structural check a caller
    can branch on. Use :func:`classify_changes` when the full change set is wanted.
    """
    return classify_changes(old, new).satisfies(mode)


def assert_evolution_allowed(
    old: MessageSchema,
    new: MessageSchema,
    mode: CompatibilityMode,
    *,
    stable_only: bool = True,
) -> CompatibilityReport:
    """The CI gate: raise if a breaking change lands on a stable channel.

    ``stable_only`` (default) honours the SemVer convention that a channel below
    ``1.0.0`` may break on a minor bump — so the gate is enforced only once the
    *old* version is stable. Pass ``stable_only=False`` to enforce regardless.

    A MAJOR version bump is an *intentional* break and is always allowed: callers
    declare "this is a new contract line" by bumping MAJOR. Returns the report on
    success so the caller can log the (allowed) change set.
    """
    report = classify_changes(old, new)

    if old.version.major != new.version.major:
        # Intentional, declared break — the new MAJOR line. Always allowed.
        return report

    if stable_only and not old.version.is_stable:
        # Pre-1.0: breaking changes are tolerated on minor bumps.
        return report

    if not report.satisfies(mode):
        offenders = report.breaking_changes(mode)
        rendered = ", ".join(f"{c.kind.value}({c.field_name})" for c in offenders)
        raise BreakingChangeError(
            f"schema {new.schema_id!r} {old.version}->{new.version} violates "
            f"{mode.value} compatibility on a stable channel: {rendered}"
        )
    return report
