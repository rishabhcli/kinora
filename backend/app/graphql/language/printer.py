"""Print an executable-document AST back to a canonical GraphQL string.

Round-trips a parsed :class:`~app.graphql.language.ast.Document` (modulo
insignificant whitespace and comments). Useful for persisted-query
normalization, error messages, and tests that assert ``parse → print → parse``
stability.
"""

from __future__ import annotations

from app.graphql.language.ast import (
    Argument,
    BooleanValue,
    Directive,
    Document,
    EnumValue,
    Field,
    FloatValue,
    FragmentDefinition,
    FragmentSpread,
    InlineFragment,
    IntValue,
    ListTypeRef,
    ListValue,
    NamedTypeRef,
    NonNullTypeRef,
    NullValue,
    ObjectValue,
    OperationDefinition,
    SelectionSet,
    StringValue,
    TypeRef,
    Value,
    Variable,
    VariableDefinition,
)

_INDENT = "  "


def print_ast(document: Document) -> str:
    """Render a document to a stable, re-parseable string."""
    return "\n\n".join(_print_definition(d) for d in document.definitions) + "\n"


def _print_definition(definition: object) -> str:
    if isinstance(definition, OperationDefinition):
        return _print_operation(definition)
    if isinstance(definition, FragmentDefinition):
        return _print_fragment(definition)
    raise TypeError(f"cannot print {type(definition).__name__}")  # pragma: no cover


def _print_operation(op: OperationDefinition) -> str:
    head = op.operation
    if op.name:
        head += f" {op.name}"
    if op.variable_definitions:
        head += "(" + ", ".join(_print_var_def(v) for v in op.variable_definitions) + ")"
    head += _print_directives(op.directives)
    is_shorthand = (
        op.operation == "query"
        and not op.name
        and not op.variable_definitions
        and not op.directives
    )
    if is_shorthand:
        return _print_selection_set(op.selection_set, 0)
    return f"{head} {_print_selection_set(op.selection_set, 0)}"


def _print_fragment(frag: FragmentDefinition) -> str:
    head = f"fragment {frag.name} on {frag.type_condition}{_print_directives(frag.directives)}"
    return f"{head} {_print_selection_set(frag.selection_set, 0)}"


def _print_var_def(var: VariableDefinition) -> str:
    out = f"${var.name}: {_print_type_ref(var.type)}"
    if var.default_value is not None:
        out += f" = {_print_value(var.default_value)}"
    return out


def _print_type_ref(ref: TypeRef) -> str:
    if isinstance(ref, NamedTypeRef):
        return ref.name
    if isinstance(ref, ListTypeRef):
        return f"[{_print_type_ref(ref.of_type)}]"
    if isinstance(ref, NonNullTypeRef):
        return f"{_print_type_ref(ref.of_type)}!"
    raise TypeError("unknown type ref")  # pragma: no cover


def _print_selection_set(selection_set: SelectionSet, depth: int) -> str:
    pad = _INDENT * (depth + 1)
    lines = [_print_selection(s, depth + 1) for s in selection_set.selections]
    body = "\n".join(f"{pad}{ln}" for ln in lines)
    closing = _INDENT * depth
    return "{\n" + body + "\n" + closing + "}"


def _print_selection(selection: object, depth: int) -> str:
    if isinstance(selection, Field):
        return _print_field(selection, depth)
    if isinstance(selection, FragmentSpread):
        return f"...{selection.name}{_print_directives(selection.directives)}"
    if isinstance(selection, InlineFragment):
        head = "..."
        if selection.type_condition:
            head += f" on {selection.type_condition}"
        head += _print_directives(selection.directives)
        return f"{head} {_print_selection_set(selection.selection_set, depth)}"
    raise TypeError("unknown selection")  # pragma: no cover


def _print_field(fld: Field, depth: int) -> str:
    out = fld.name if not fld.alias else f"{fld.alias}: {fld.name}"
    if fld.arguments:
        out += "(" + ", ".join(_print_argument(a) for a in fld.arguments) + ")"
    out += _print_directives(fld.directives)
    if fld.selection_set is not None:
        out += " " + _print_selection_set(fld.selection_set, depth)
    return out


def _print_argument(arg: Argument) -> str:
    return f"{arg.name}: {_print_value(arg.value)}"


def _print_directives(directives: tuple[Directive, ...]) -> str:
    if not directives:
        return ""
    parts = []
    for d in directives:
        s = f"@{d.name}"
        if d.arguments:
            s += "(" + ", ".join(_print_argument(a) for a in d.arguments) + ")"
        parts.append(s)
    return " " + " ".join(parts)


def _print_value(value: Value) -> str:
    if isinstance(value, Variable):
        return f"${value.name}"
    if isinstance(value, IntValue):
        return value.value
    if isinstance(value, FloatValue):
        return value.value
    if isinstance(value, StringValue):
        return _print_string(value.value)
    if isinstance(value, BooleanValue):
        return "true" if value.value else "false"
    if isinstance(value, NullValue):
        return "null"
    if isinstance(value, EnumValue):
        return value.value
    if isinstance(value, ListValue):
        return "[" + ", ".join(_print_value(v) for v in value.values) + "]"
    if isinstance(value, ObjectValue):
        return "{" + ", ".join(f"{f.name}: {_print_value(f.value)}" for f in value.fields) + "}"
    raise TypeError("unknown value")  # pragma: no cover


def _print_string(raw: str) -> str:
    escaped = (
        raw.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


__all__ = ["print_ast"]
