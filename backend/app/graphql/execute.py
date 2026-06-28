"""The async GraphQL executor.

Given a validated :class:`~app.graphql.language.ast.Document`, an operation name,
variables, a :class:`~app.graphql.schema.Schema`, and a per-request context, it:

1. coerces the operation's variables against their declared types;
2. resolves the root selection set field-by-field (mutations serially, queries
   concurrently);
3. for composite return types, recurses into the sub-selection (applying
   ``@skip``/``@include`` directives and fragment type-conditions);
4. completes leaf values through their scalar/enum ``serialize``;
5. propagates ``null`` up to the nearest nullable field on error, collecting a
   masked :class:`~app.graphql.errors.GraphQLError` per failure (the spec's
   error-bubbling semantics).

Resolvers may be sync or ``async``; ``None`` source field access falls back to
attribute/key lookup so plain dicts and dataclasses work without boilerplate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.graphql.errors import ErrorCode, GraphQLError, bad_input, mask_error
from app.graphql.language.ast import (
    BooleanValue,
    Directive,
    EnumValue,
    Field,
    FloatValue,
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
    Selection,
    SelectionSet,
    StringValue,
    TypeRef,
    Variable,
)
from app.graphql.language.ast import (
    Document as DocumentNode,
)
from app.graphql.schema import Schema
from app.graphql.type_system import (
    UNDEFINED,
    GraphQLEnum,
    GraphQLInterface,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
    GraphQLType,
    GraphQLUnion,
    coerce_input,
)


@dataclass(slots=True)
class ResolveInfo:
    """Diagnostic context passed to a resolver as its fourth argument."""

    field_name: str
    parent_type: str
    return_type: GraphQLType
    path: list[str | int]
    schema: Schema
    variables: dict[str, Any]


@dataclass(slots=True)
class ExecutionResult:
    """The outcome of one execution: ``data`` and/or collected ``errors``."""

    data: dict[str, Any] | None = None
    errors: list[GraphQLError] = field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.errors:
            out["errors"] = [e.to_dict() for e in self.errors]
        if self.data is not None or not self.errors:
            out["data"] = self.data
        return out


class _Stop(Exception):  # noqa: N818 - internal control flow, not a public error
    """Internal: a non-null violation that nulls the nearest nullable ancestor."""


class _Executor:
    def __init__(
        self,
        schema: Schema,
        document: DocumentNode,
        *,
        variables: Mapping[str, Any],
        context: Any,
        root_value: Any,
    ) -> None:
        self.schema = schema
        self.document = document
        self.fragments = document.fragments()
        self.context = context
        self.root_value = root_value
        self.errors: list[GraphQLError] = []
        self._raw_variables = dict(variables)
        self.variables: dict[str, Any] = {}

    async def execute(self, operation: OperationDefinition) -> ExecutionResult:
        root_type = self.schema.root_for(operation.operation)
        if root_type is None:
            return ExecutionResult(
                data=None,
                errors=[
                    GraphQLError(
                        f"Schema has no {operation.operation} root type.",
                        code=ErrorCode.GRAPHQL_VALIDATION_FAILED,
                    )
                ],
            )
        self.variables = self._coerce_variables(operation)
        serial = operation.operation == "mutation"
        try:
            data = await self._execute_selection_set(
                operation.selection_set,
                root_type,
                self.root_value,
                path=[],
                serial=serial,
            )
        except _Stop:
            data = None
        return ExecutionResult(data=data, errors=self.errors)

    # -- variable coercion --------------------------------------------------- #

    def _coerce_variables(self, operation: OperationDefinition) -> dict[str, Any]:
        coerced: dict[str, Any] = {}
        for var_def in operation.variable_definitions:
            gql_type = self._resolve_type_ref(var_def.type)
            if var_def.name in self._raw_variables:
                raw = self._raw_variables[var_def.name]
                coerced[var_def.name] = coerce_input(gql_type, raw, path=f"${var_def.name}")
            elif var_def.default_value is not None:
                coerced[var_def.name] = coerce_input(
                    gql_type,
                    self._literal_to_value(var_def.default_value, {}),
                    path=f"${var_def.name}",
                )
            elif isinstance(gql_type, GraphQLNonNull):
                raise bad_input(f"Variable ${var_def.name} of required type is missing.")
            else:
                coerced[var_def.name] = UNDEFINED
        return coerced

    def _resolve_type_ref(self, ref: TypeRef) -> GraphQLType:
        if isinstance(ref, NonNullTypeRef):
            return GraphQLNonNull(self._resolve_type_ref(ref.of_type))
        if isinstance(ref, ListTypeRef):
            return GraphQLList(self._resolve_type_ref(ref.of_type))
        if isinstance(ref, NamedTypeRef):
            named = self.schema.get_type(ref.name)
            if named is None:
                raise bad_input(f"Unknown type {ref.name!r} in variable definition.")
            return named
        raise bad_input("Unknown type reference.")  # pragma: no cover

    # -- selection set ------------------------------------------------------- #

    async def _execute_selection_set(
        self,
        selection_set: SelectionSet,
        object_type: GraphQLObject,
        source: Any,
        *,
        path: list[str | int],
        serial: bool = False,
    ) -> dict[str, Any]:
        grouped = self._collect_fields(selection_set, object_type, source)
        result: dict[str, Any] = {}
        if serial:
            for response_key, fields in grouped.items():
                result[response_key] = await self._execute_field(
                    object_type, source, fields, path + [response_key]
                )
            return result
        # Queries: resolve sibling fields concurrently.
        keys = list(grouped)
        values = await asyncio.gather(
            *(
                self._execute_field(object_type, source, grouped[k], path + [k])
                for k in keys
            )
        )
        return dict(zip(keys, values, strict=True))

    def _collect_fields(
        self,
        selection_set: SelectionSet,
        object_type: GraphQLObject,
        source: Any,
        visited: set[str] | None = None,
    ) -> dict[str, list[Field]]:
        visited = visited if visited is not None else set()
        grouped: dict[str, list[Field]] = {}
        for selection in selection_set.selections:
            if not self._should_include(selection):
                continue
            if isinstance(selection, Field):
                grouped.setdefault(selection.response_key, []).append(selection)
            elif isinstance(selection, InlineFragment):
                if self._fragment_applies(selection.type_condition, object_type, source):
                    self._merge(
                        grouped,
                        self._collect_fields(
                            selection.selection_set, object_type, source, visited
                        ),
                    )
            elif isinstance(selection, FragmentSpread):
                if selection.name in visited:
                    continue
                visited.add(selection.name)
                frag = self.fragments.get(selection.name)
                if frag is None:
                    continue
                if self._fragment_applies(frag.type_condition, object_type, source):
                    self._merge(
                        grouped,
                        self._collect_fields(
                            frag.selection_set, object_type, source, visited
                        ),
                    )
        return grouped

    @staticmethod
    def _merge(into: dict[str, list[Field]], more: dict[str, list[Field]]) -> None:
        for key, fields in more.items():
            into.setdefault(key, []).extend(fields)

    def _fragment_applies(
        self, type_condition: str | None, object_type: GraphQLObject, source: Any
    ) -> bool:
        if type_condition is None:
            return True
        return self.schema.is_possible_type(type_condition, object_type.name)

    def _should_include(self, selection: Selection) -> bool:
        directives = getattr(selection, "directives", ())
        for directive in directives:
            if directive.name == "skip" and self._directive_if(directive):
                return False
            if directive.name == "include" and not self._directive_if(directive):
                return False
        return True

    def _directive_if(self, directive: Directive) -> bool:
        for arg in directive.arguments:
            if arg.name == "if":
                value = self._literal_to_value(arg.value, self.variables)
                return bool(value)
        return False

    # -- field execution ----------------------------------------------------- #

    async def _execute_field(
        self,
        object_type: GraphQLObject,
        source: Any,
        fields: list[Field],
        path: list[str | int],
    ) -> Any:
        field_node = fields[0]
        if field_node.name == "__typename":
            # The introspection meta-field is valid on every object/abstract type
            # and resolves to the runtime concrete type name.
            return object_type.name
        field_def = object_type.fields.get(field_node.name)
        if field_def is None:
            # Validation should have caught this; bubble a null defensively.
            self.errors.append(
                GraphQLError(
                    f"Cannot query field {field_node.name!r} on type {object_type.name!r}.",
                    code=ErrorCode.GRAPHQL_VALIDATION_FAILED,
                    path=list(path),
                ).with_path(list(path))
            )
            return None
        try:
            args = self._coerce_field_args(field_def, field_node, path)
            info = ResolveInfo(
                field_name=field_node.name,
                parent_type=object_type.name,
                return_type=field_def.type,
                path=list(path),
                schema=self.schema,
                variables=self.variables,
            )
            resolver = field_def.resolver or _default_resolver
            raw = resolver(source, args, self.context, info)
            if asyncio.iscoroutine(raw):
                raw = await raw
            return await self._complete_value(field_def.type, fields, raw, path)
        except _Stop:
            raise
        except Exception as exc:  # noqa: BLE001 - mask + collect, then bubble null
            self.errors.append(mask_error(exc, path=list(path)))
            if isinstance(field_def.type, GraphQLNonNull):
                raise _Stop from exc
            return None

    def _coerce_field_args(
        self, field_def: Any, field_node: Field, path: list[str | int]
    ) -> dict[str, Any]:
        provided = {a.name: a for a in field_node.arguments}
        out: dict[str, Any] = {}
        for arg_name, arg_def in field_def.args.items():
            if arg_name in provided:
                arg_value = provided[arg_name].value
                literal = self._literal_to_value(arg_value, self.variables)
                if literal is UNDEFINED:
                    if arg_def.default_value is not UNDEFINED:
                        out[arg_name] = arg_def.default_value
                    elif isinstance(arg_def.type, GraphQLNonNull):
                        raise bad_input(f"Required argument {arg_name!r} was not provided.")
                    else:
                        out[arg_name] = None
                    continue
                # A whole-argument variable was already coerced against its declared
                # type at the variables stage; re-coercing the internal value would
                # double-coerce (e.g. an enum's internal value would fail re-parse).
                if isinstance(arg_value, Variable):
                    out[arg_name] = literal
                else:
                    out[arg_name] = coerce_input(arg_def.type, literal, path=arg_name)
            elif arg_def.default_value is not UNDEFINED:
                out[arg_name] = arg_def.default_value
            elif isinstance(arg_def.type, GraphQLNonNull):
                raise bad_input(f"Required argument {arg_name!r} was not provided.")
            else:
                out[arg_name] = None
        return out

    # -- value completion ---------------------------------------------------- #

    async def _complete_value(
        self,
        return_type: GraphQLType,
        fields: list[Field],
        value: Any,
        path: list[str | int],
    ) -> Any:
        if isinstance(return_type, GraphQLNonNull):
            completed = await self._complete_value(return_type.of_type, fields, value, path)
            if completed is None:
                self.errors.append(
                    GraphQLError(
                        f"Cannot return null for non-nullable field at {_fmt_path(path)}.",
                        code=ErrorCode.INTERNAL_SERVER_ERROR,
                        path=list(path),
                    )
                )
                raise _Stop
            return completed
        if value is None:
            return None
        if isinstance(return_type, GraphQLList):
            return await self._complete_list(return_type, fields, value, path)
        if isinstance(return_type, (GraphQLScalar, GraphQLEnum)):
            return self._complete_leaf(return_type, value, path)
        if isinstance(return_type, GraphQLObject):
            return await self._complete_object(return_type, fields, value, path)
        if isinstance(return_type, (GraphQLInterface, GraphQLUnion)):
            object_type = self._resolve_abstract_type(return_type, value)
            return await self._complete_object(object_type, fields, value, path)
        raise GraphQLError(f"Unhandled return type {return_type!s}.")

    async def _complete_list(
        self,
        return_type: GraphQLList,
        fields: list[Field],
        value: Any,
        path: list[str | int],
    ) -> list[Any] | None:
        if not isinstance(value, (list, tuple)):
            self.errors.append(
                GraphQLError(
                    f"Expected a list at {_fmt_path(path)}.",
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    path=list(path),
                )
            )
            return None
        inner = return_type.of_type
        results = []
        for index, item in enumerate(value):
            item_path = path + [index]
            try:
                results.append(await self._complete_value(inner, fields, item, item_path))
            except _Stop:
                # A non-null list item failed: the whole list becomes null only if
                # the list's item type is itself non-null (handled by re-raising).
                if isinstance(inner, GraphQLNonNull):
                    return None
                results.append(None)
        return results

    def _complete_leaf(
        self, return_type: GraphQLScalar | GraphQLEnum, value: Any, path: list[str | int]
    ) -> Any:
        try:
            return return_type.serialize(value)
        except GraphQLError as exc:
            self.errors.append(exc.with_path(list(path)))
            return None

    async def _complete_object(
        self,
        object_type: GraphQLObject,
        fields: list[Field],
        value: Any,
        path: list[str | int],
    ) -> dict[str, Any] | None:
        merged = _merge_selection_sets(fields)
        if merged is None:
            return {}
        return await self._execute_selection_set(merged, object_type, value, path=path)

    def _resolve_abstract_type(
        self, abstract: GraphQLInterface | GraphQLUnion, value: Any
    ) -> GraphQLObject:
        resolver = abstract.resolve_type
        type_name: str | None = None
        if resolver is not None:
            type_name = resolver(value)
        elif isinstance(value, Mapping) and "__typename" in value:
            type_name = str(value["__typename"])
        if type_name is None:
            raise GraphQLError(
                f"Could not resolve concrete type for abstract type {abstract.name!r}."
            )
        resolved = self.schema.get_type(type_name)
        if not isinstance(resolved, GraphQLObject):
            raise GraphQLError(f"Abstract type resolved to non-object {type_name!r}.")
        return resolved

    # -- literal → python value (resolving variables) ----------------------- #

    def _literal_to_value(self, node: Any, variables: Mapping[str, Any]) -> Any:
        if isinstance(node, Variable):
            if node.name not in variables:
                return UNDEFINED
            return variables[node.name]
        if isinstance(node, IntValue):
            return int(node.value)
        if isinstance(node, FloatValue):
            return float(node.value)
        if isinstance(node, StringValue):
            return node.value
        if isinstance(node, BooleanValue):
            return node.value
        if isinstance(node, NullValue):
            return None
        if isinstance(node, EnumValue):
            return node.value
        if isinstance(node, ListValue):
            out = [self._literal_to_value(v, variables) for v in node.values]
            return [v for v in out if v is not UNDEFINED]
        if isinstance(node, ObjectValue):
            obj: dict[str, Any] = {}
            for f in node.fields:
                v = self._literal_to_value(f.value, variables)
                if v is not UNDEFINED:
                    obj[f.name] = v
            return obj
        return UNDEFINED


def _default_resolver(source: Any, args: dict[str, Any], context: Any, info: ResolveInfo) -> Any:
    """Fall back to attribute/key access for ``info.field_name`` on ``source``."""
    field_name = info.field_name
    if isinstance(source, Mapping):
        return source.get(field_name)
    return getattr(source, field_name, None)


def _merge_selection_sets(fields: list[Field]) -> SelectionSet | None:
    selections: list[Selection] = []
    for fld in fields:
        if fld.selection_set is not None:
            selections.extend(fld.selection_set.selections)
    if not selections:
        return None
    return SelectionSet(tuple(selections))


def _fmt_path(path: list[str | int]) -> str:
    return ".".join(str(p) for p in path) or "<root>"


def select_operation(
    document: DocumentNode, operation_name: str | None
) -> OperationDefinition:
    """Pick the operation to run (the spec's operation-selection rule)."""
    operations = document.operations()
    if not operations:
        raise GraphQLError(
            "Document contains no operations.", code=ErrorCode.GRAPHQL_VALIDATION_FAILED
        )
    if operation_name is None:
        if len(operations) != 1:
            raise GraphQLError(
                "Must provide operationName when the document defines multiple operations.",
                code=ErrorCode.GRAPHQL_VALIDATION_FAILED,
            )
        return operations[0]
    for op in operations:
        if op.name == operation_name:
            return op
    raise GraphQLError(
        f"Unknown operation named {operation_name!r}.",
        code=ErrorCode.GRAPHQL_VALIDATION_FAILED,
    )


async def execute(
    schema: Schema,
    document: DocumentNode,
    *,
    operation_name: str | None = None,
    variables: Mapping[str, Any] | None = None,
    context: Any = None,
    root_value: Any = None,
) -> ExecutionResult:
    """Execute one operation of a document, returning data + collected errors."""
    try:
        operation = select_operation(document, operation_name)
    except GraphQLError as exc:
        return ExecutionResult(data=None, errors=[exc])
    executor = _Executor(
        schema,
        document,
        variables=variables or {},
        context=context,
        root_value=root_value,
    )
    try:
        return await executor.execute(operation)
    except GraphQLError as exc:
        # A variable-coercion / pre-resolution failure: surface it as the only error.
        executor.errors.append(exc)
        return ExecutionResult(data=None, errors=executor.errors)


__all__ = [
    "ExecutionResult",
    "ResolveInfo",
    "execute",
    "select_operation",
]
