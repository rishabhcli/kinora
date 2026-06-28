"""Pre-execution validation: structure, limits, and cost.

A public endpoint must reject hostile or malformed queries *before* touching the
database. This module runs a focused set of checks against the parsed document
and the schema:

* **single-anonymous / lone-anonymous** operation rules;
* **field existence** — every selected field exists on its parent type;
* **argument existence + required args** — unknown/missing required arguments;
* **leaf/composite** mismatch — a scalar must not have a sub-selection, a
  composite must;
* **fragment** checks — spreads reference defined fragments, no spread cycles,
  type conditions name real composite types;
* **depth limit** — reject queries nested deeper than ``max_depth``;
* **complexity/cost limit** — sum each field's static ``cost`` (list fields
  scaled by the effective ``first`` page size) and reject over ``max_cost``;
* **node-count / alias** guards against sheer query bulk.

Each problem is collected as a :class:`~app.graphql.errors.GraphQLError`; a
non-empty list means "do not execute".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.graphql.errors import ErrorCode, GraphQLError
from app.graphql.language.ast import (
    Document,
    Field,
    FragmentDefinition,
    FragmentSpread,
    InlineFragment,
    IntValue,
    OperationDefinition,
    SelectionSet,
    Variable,
)
from app.graphql.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.graphql.schema import Schema
from app.graphql.type_system import (
    GraphQLEnum as _Enum,
)
from app.graphql.type_system import (
    GraphQLInterface,
    GraphQLNonNull,
    GraphQLObject,
    GraphQLScalar,
    GraphQLType,
    GraphQLUnion,
)


@dataclass(slots=True)
class ValidationLimits:
    """Tunable limits enforced by :func:`validate`."""

    max_depth: int = 12
    max_cost: int = 1000
    max_aliases: int = 200
    max_nodes: int = 2000


DEFAULT_LIMITS = ValidationLimits()


def validate(
    schema: Schema, document: Document, *, limits: ValidationLimits | None = None
) -> list[GraphQLError]:
    """Validate a document; return collected errors ([] => safe to execute)."""
    limits = limits or DEFAULT_LIMITS
    return _Validator(schema, document, limits).run()


class _Validator:
    def __init__(self, schema: Schema, document: Document, limits: ValidationLimits) -> None:
        self.schema = schema
        self.document = document
        self.limits = limits
        self.fragments = document.fragments()
        self.errors: list[GraphQLError] = []
        self._node_count = 0
        self._alias_count = 0

    def run(self) -> list[GraphQLError]:
        self._check_operations()
        self._check_fragment_cycles()
        for op in self.document.operations():
            root = self.schema.root_for(op.operation)
            if root is None:
                self._error(f"Schema has no {op.operation} root type.")
                continue
            self._check_selection_set(op.selection_set, root, depth=1)
        if self._node_count > self.limits.max_nodes:
            self._error(
                f"Query has too many nodes ({self._node_count} > {self.limits.max_nodes}).",
                code=ErrorCode.COMPLEXITY_LIMIT_EXCEEDED,
            )
        if self._alias_count > self.limits.max_aliases:
            self._error(
                f"Query has too many aliases ({self._alias_count} > {self.limits.max_aliases}).",
                code=ErrorCode.COMPLEXITY_LIMIT_EXCEEDED,
            )
        # Depth + cost are checked once over each operation (need no per-field walk
        # contamination from the structural walk above).
        for op in self.document.operations():
            root = self.schema.root_for(op.operation)
            if root is None:
                continue
            depth = self._selection_depth(op.selection_set, root, set())
            if depth > self.limits.max_depth:
                self._error(
                    f"Query is too deep ({depth} > {self.limits.max_depth}).",
                    code=ErrorCode.DEPTH_LIMIT_EXCEEDED,
                )
            cost = self._selection_cost(op.selection_set, root, set())
            if cost > self.limits.max_cost:
                self._error(
                    f"Query is too complex (cost {cost} > {self.limits.max_cost}).",
                    code=ErrorCode.COMPLEXITY_LIMIT_EXCEEDED,
                )
        return self.errors

    # -- operations + fragments --------------------------------------------- #

    def _check_operations(self) -> None:
        operations = self.document.operations()
        anonymous = [op for op in operations if op.name is None]
        if anonymous and len(operations) > 1:
            self._error("This document contains an anonymous operation alongside others.")
        names = [op.name for op in operations if op.name]
        if len(names) != len(set(names)):
            self._error("Operations must have unique names.")
        frag_names = [
            f.name for f in self.document.definitions if isinstance(f, FragmentDefinition)
        ]
        if len(frag_names) != len(set(frag_names)):
            self._error("Fragments must have unique names.")

    def _check_fragment_cycles(self) -> None:
        for name in self.fragments:
            self._detect_cycle(name, [name], set())

    def _detect_cycle(self, name: str, stack: list[str], seen: set[str]) -> None:
        frag = self.fragments.get(name)
        if frag is None:
            return
        for spread in _spreads_in(frag.selection_set):
            if spread == name or spread in stack:
                self._error(f"Fragment spread {spread!r} forms a cycle.")
                continue
            if spread in seen:
                continue
            seen.add(spread)
            self._detect_cycle(spread, [*stack, spread], seen)

    # -- structural walk ----------------------------------------------------- #

    def _check_selection_set(
        self,
        selection_set: SelectionSet,
        parent: GraphQLType,
        depth: int,
        fragment_stack: frozenset[str] = frozenset(),
    ) -> None:
        named = parent.unwrap()
        for selection in selection_set.selections:
            self._node_count += 1
            if isinstance(selection, Field):
                self._check_field(selection, named, depth, fragment_stack)
            elif isinstance(selection, InlineFragment):
                self._check_inline_fragment(selection, named, depth, fragment_stack)
            elif isinstance(selection, FragmentSpread):
                self._check_fragment_spread(selection, named, fragment_stack)

    def _check_field(
        self,
        fld: Field,
        parent: GraphQLType,
        depth: int,
        fragment_stack: frozenset[str] = frozenset(),
    ) -> None:
        if fld.alias:
            self._alias_count += 1
        # Introspection meta-fields are always valid.
        if fld.name in {"__typename"}:
            return
        if not isinstance(parent, (GraphQLObject, GraphQLInterface)):
            self._error(
                f"Cannot select field {fld.name!r} on type {parent.name!r}.",
                node=fld,
            )
            return
        if fld.name in {"__schema", "__type"} and parent is self.schema.query:
            return  # introspection roots
        field_def = parent.fields.get(fld.name)
        if field_def is None:
            self._error(
                f"Cannot query field {fld.name!r} on type {parent.name!r}.",
                code=ErrorCode.GRAPHQL_VALIDATION_FAILED,
                node=fld,
            )
            return
        self._check_arguments(fld, field_def, parent)
        return_named = field_def.type.unwrap()
        is_leaf = isinstance(return_named, (GraphQLScalar, _Enum))
        if is_leaf and fld.selection_set is not None:
            self._error(
                f"Field {fld.name!r} returns a scalar and cannot have a sub-selection.",
                node=fld,
            )
        elif not is_leaf and fld.selection_set is None:
            self._error(
                f"Field {fld.name!r} returns a composite type and requires a sub-selection.",
                node=fld,
            )
        if fld.selection_set is not None and not is_leaf:
            self._check_selection_set(
                fld.selection_set, field_def.type, depth + 1, fragment_stack
            )

    def _check_arguments(self, fld: Field, field_def: object, parent: GraphQLType) -> None:
        defined = getattr(field_def, "args", {})
        provided = {a.name for a in fld.arguments}
        for arg in fld.arguments:
            if arg.name not in defined:
                self._error(
                    f"Unknown argument {arg.name!r} on field {parent.name}.{fld.name}.",
                    node=fld,
                )
                continue
            # Variable references are checked at execution (variable coercion).
            if isinstance(arg.value, Variable):
                continue
        for arg_name, arg_def in defined.items():
            from app.graphql.type_system import UNDEFINED

            if (
                isinstance(arg_def.type, GraphQLNonNull)
                and arg_def.default_value is UNDEFINED
                and arg_name not in provided
            ):
                self._error(
                    f"Field {parent.name}.{fld.name} is missing required argument {arg_name!r}.",
                    node=fld,
                )

    def _check_inline_fragment(
        self,
        frag: InlineFragment,
        parent: GraphQLType,
        depth: int,
        fragment_stack: frozenset[str] = frozenset(),
    ) -> None:
        target = parent
        if frag.type_condition is not None:
            named = self.schema.get_type(frag.type_condition)
            if named is None or not isinstance(
                named, (GraphQLObject, GraphQLInterface, GraphQLUnion)
            ):
                self._error(f"Unknown type {frag.type_condition!r} in inline fragment.")
                return
            target = named
        self._check_selection_set(frag.selection_set, target, depth + 1, fragment_stack)

    def _check_fragment_spread(
        self,
        spread: FragmentSpread,
        parent: GraphQLType,
        fragment_stack: frozenset[str] = frozenset(),
    ) -> None:
        if spread.name in fragment_stack:
            # A cycle — reported separately by ``_check_fragment_cycles``; stop the
            # structural walk here so it terminates instead of recursing forever.
            return
        frag = self.fragments.get(spread.name)
        if frag is None:
            self._error(f"Unknown fragment {spread.name!r}.")
            return
        named = self.schema.get_type(frag.type_condition)
        if named is None:
            self._error(f"Unknown type {frag.type_condition!r} in fragment {spread.name!r}.")
            return
        self._check_selection_set(
            frag.selection_set, named, depth=1, fragment_stack=fragment_stack | {spread.name}
        )

    # -- depth + cost (independent passes) ---------------------------------- #

    def _selection_depth(
        self, selection_set: SelectionSet, parent: GraphQLType, seen: set[str]
    ) -> int:
        named = parent.unwrap()
        best = 0
        for selection in selection_set.selections:
            if isinstance(selection, Field):
                if selection.name.startswith("__"):
                    continue
                if not isinstance(named, (GraphQLObject, GraphQLInterface)):
                    continue
                field_def = named.fields.get(selection.name)
                if field_def is None or selection.selection_set is None:
                    best = max(best, 1)
                    continue
                child = 1 + self._selection_depth(
                    selection.selection_set, field_def.type, seen
                )
                best = max(best, child)
            elif isinstance(selection, InlineFragment):
                target = named
                if selection.type_condition is not None:
                    found = self.schema.get_type(selection.type_condition)
                    if found is not None:
                        target = found
                best = max(best, self._selection_depth(selection.selection_set, target, seen))
            elif isinstance(selection, FragmentSpread):
                if selection.name in seen:
                    continue
                frag = self.fragments.get(selection.name)
                if frag is None:
                    continue
                target = self.schema.get_type(frag.type_condition) or named
                best = max(
                    best,
                    self._selection_depth(frag.selection_set, target, seen | {selection.name}),
                )
        return best

    def _selection_cost(
        self, selection_set: SelectionSet, parent: GraphQLType, seen: set[str]
    ) -> int:
        named = parent.unwrap()
        total = 0
        for selection in selection_set.selections:
            if isinstance(selection, Field):
                if selection.name.startswith("__"):
                    continue
                if not isinstance(named, (GraphQLObject, GraphQLInterface)):
                    continue
                field_def = named.fields.get(selection.name)
                if field_def is None:
                    total += 1
                    continue
                child = 0
                if selection.selection_set is not None:
                    child = self._selection_cost(
                        selection.selection_set, field_def.type, seen
                    )
                multiplier = 1
                if field_def.list_cost_multiplier:
                    multiplier = _page_size_of(selection)
                total += field_def.cost + multiplier * child
            elif isinstance(selection, InlineFragment):
                target = named
                if selection.type_condition is not None:
                    target = self.schema.get_type(selection.type_condition) or named
                total += self._selection_cost(selection.selection_set, target, seen)
            elif isinstance(selection, FragmentSpread):
                if selection.name in seen:
                    continue
                frag = self.fragments.get(selection.name)
                if frag is None:
                    continue
                target = self.schema.get_type(frag.type_condition) or named
                total += self._selection_cost(
                    frag.selection_set, target, seen | {selection.name}
                )
        return total

    # -- error helper -------------------------------------------------------- #

    def _error(
        self,
        message: str,
        *,
        code: str = ErrorCode.GRAPHQL_VALIDATION_FAILED,
        node: Field | None = None,
    ) -> None:
        locations = None
        if node is not None and node.line:
            locations = [(node.line, node.column)]
        self.errors.append(GraphQLError(message, code=code, locations=locations))


def _page_size_of(field: Field) -> int:
    """The effective ``first`` for a paginated field's cost multiplier."""
    for arg in field.arguments:
        if arg.name == "first" and isinstance(arg.value, IntValue):
            try:
                return max(1, min(int(arg.value.value), MAX_PAGE_SIZE))
            except ValueError:  # pragma: no cover
                return DEFAULT_PAGE_SIZE
    return DEFAULT_PAGE_SIZE


def _spreads_in(selection_set: SelectionSet) -> list[str]:
    out: list[str] = []
    for selection in selection_set.selections:
        if isinstance(selection, FragmentSpread):
            out.append(selection.name)
            continue
        child = selection.selection_set if isinstance(selection, (Field, InlineFragment)) else None
        if child is not None:
            out.extend(_spreads_in(child))
    return out


def estimate_cost(schema: Schema, document: Document, operation: OperationDefinition) -> int:
    """Public helper: the static cost of one operation (for metrics/headers)."""
    validator = _Validator(schema, document, DEFAULT_LIMITS)
    root = schema.root_for(operation.operation)
    if root is None:
        return 0
    return validator._selection_cost(operation.selection_set, root, set())


__all__ = ["DEFAULT_LIMITS", "ValidationLimits", "estimate_cost", "validate"]
