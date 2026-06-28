"""GraphQL executable-document AST nodes.

Only the executable subset is modelled (operations + fragments); the gateway's
schema is defined in code (``app/graphql/type_system.py``), not parsed from SDL,
so there are no type-system AST nodes here. ``Value`` nodes keep their literal
shape so coercion against the schema happens in the executor (where variable
values are available), not in the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Variable:
    """A ``$name`` reference to a query variable."""

    name: str


@dataclass(frozen=True, slots=True)
class IntValue:
    value: str


@dataclass(frozen=True, slots=True)
class FloatValue:
    value: str


@dataclass(frozen=True, slots=True)
class StringValue:
    value: str
    block: bool = False


@dataclass(frozen=True, slots=True)
class BooleanValue:
    value: bool


@dataclass(frozen=True, slots=True)
class NullValue:
    pass


@dataclass(frozen=True, slots=True)
class EnumValue:
    value: str


@dataclass(frozen=True, slots=True)
class ListValue:
    values: tuple[Value, ...]


@dataclass(frozen=True, slots=True)
class ObjectField:
    name: str
    value: Value


@dataclass(frozen=True, slots=True)
class ObjectValue:
    fields: tuple[ObjectField, ...]


Value = (
    Variable
    | IntValue
    | FloatValue
    | StringValue
    | BooleanValue
    | NullValue
    | EnumValue
    | ListValue
    | ObjectValue
)


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    value: Value
    line: int = 0
    column: int = 0


@dataclass(frozen=True, slots=True)
class Directive:
    name: str
    arguments: tuple[Argument, ...] = ()


# -- type references (used only inside variable definitions) ----------------- #


@dataclass(frozen=True, slots=True)
class NamedTypeRef:
    name: str


@dataclass(frozen=True, slots=True)
class ListTypeRef:
    of_type: TypeRef


@dataclass(frozen=True, slots=True)
class NonNullTypeRef:
    of_type: TypeRef


TypeRef = NamedTypeRef | ListTypeRef | NonNullTypeRef


@dataclass(frozen=True, slots=True)
class VariableDefinition:
    name: str
    type: TypeRef
    default_value: Value | None = None


# -- selections -------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    alias: str | None = None
    arguments: tuple[Argument, ...] = ()
    directives: tuple[Directive, ...] = ()
    selection_set: SelectionSet | None = None
    line: int = 0
    column: int = 0

    @property
    def response_key(self) -> str:
        """The key this field's value is stored under in the response object."""
        return self.alias or self.name


@dataclass(frozen=True, slots=True)
class FragmentSpread:
    name: str
    directives: tuple[Directive, ...] = ()


@dataclass(frozen=True, slots=True)
class InlineFragment:
    type_condition: str | None
    selection_set: SelectionSet
    directives: tuple[Directive, ...] = ()


Selection = Field | FragmentSpread | InlineFragment


@dataclass(frozen=True, slots=True)
class SelectionSet:
    selections: tuple[Selection, ...]


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    operation: str  # "query" | "mutation" | "subscription"
    selection_set: SelectionSet
    name: str | None = None
    variable_definitions: tuple[VariableDefinition, ...] = ()
    directives: tuple[Directive, ...] = ()


@dataclass(frozen=True, slots=True)
class FragmentDefinition:
    name: str
    type_condition: str
    selection_set: SelectionSet
    directives: tuple[Directive, ...] = ()


Definition = OperationDefinition | FragmentDefinition


@dataclass(frozen=True, slots=True)
class Document:
    definitions: tuple[Definition, ...] = field(default=())

    def operations(self) -> list[OperationDefinition]:
        return [d for d in self.definitions if isinstance(d, OperationDefinition)]

    def fragments(self) -> dict[str, FragmentDefinition]:
        return {
            d.name: d for d in self.definitions if isinstance(d, FragmentDefinition)
        }


__all__ = [
    "Argument",
    "BooleanValue",
    "Definition",
    "Directive",
    "Document",
    "EnumValue",
    "Field",
    "FloatValue",
    "FragmentDefinition",
    "FragmentSpread",
    "InlineFragment",
    "IntValue",
    "ListTypeRef",
    "ListValue",
    "NamedTypeRef",
    "NonNullTypeRef",
    "NullValue",
    "ObjectField",
    "ObjectValue",
    "OperationDefinition",
    "Selection",
    "SelectionSet",
    "StringValue",
    "TypeRef",
    "Value",
    "Variable",
    "VariableDefinition",
]
