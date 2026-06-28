"""A recursive-descent parser for GraphQL executable documents.

Parses operations (query/mutation/subscription), named + anonymous operations,
variable definitions with default values, fragments (named + inline), field
arguments, directives, and the full literal grammar into the AST in
``app/graphql/language/ast.py``. Type-system (SDL) definitions are intentionally
out of scope — the gateway schema is built in code, so only executable documents
are accepted (a stray ``type``/``schema`` keyword is a clear syntax error).
"""

from __future__ import annotations

from app.graphql.language.ast import (
    Argument,
    BooleanValue,
    Definition,
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
    ObjectField,
    ObjectValue,
    OperationDefinition,
    Selection,
    SelectionSet,
    StringValue,
    TypeRef,
    Value,
    Variable,
    VariableDefinition,
)
from app.graphql.language.lexer import Lexer, LexError, Token, TokenKind

_OPERATION_KEYWORDS = {"query", "mutation", "subscription"}


class GraphQLSyntaxError(Exception):
    """A syntax error with a 1-based ``line``/``column`` source position."""

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"Syntax Error: {message} (at {line}:{column})")
        self.message = message
        self.line = line
        self.column = column


class _Parser:
    def __init__(self, source: str) -> None:
        try:
            self._tokens = Lexer(source).tokens()
        except LexError as exc:
            raise GraphQLSyntaxError(exc.message, exc.line, exc.column) from exc
        self._idx = 0

    # -- token cursor -------------------------------------------------------- #

    @property
    def _cur(self) -> Token:
        return self._tokens[self._idx]

    def _advance(self) -> Token:
        tok = self._tokens[self._idx]
        if tok.kind is not TokenKind.EOF:
            self._idx += 1
        return tok

    def _expect(self, kind: TokenKind) -> Token:
        tok = self._cur
        if tok.kind is not kind:
            raise self._err(f"expected {kind.value!r} but found {self._describe(tok)}")
        return self._advance()

    def _expect_keyword(self, word: str) -> Token:
        tok = self._cur
        if tok.kind is not TokenKind.NAME or tok.value != word:
            raise self._err(f"expected {word!r} but found {self._describe(tok)}")
        return self._advance()

    def _is(self, kind: TokenKind) -> bool:
        return self._cur.kind is kind

    def _err(self, message: str) -> GraphQLSyntaxError:
        tok = self._cur
        return GraphQLSyntaxError(message, tok.line, tok.column)

    @staticmethod
    def _describe(tok: Token) -> str:
        if tok.kind is TokenKind.EOF:
            return "<EOF>"
        if tok.kind in {TokenKind.NAME, TokenKind.INT, TokenKind.FLOAT}:
            return f"{tok.value!r}"
        if tok.kind in {TokenKind.STRING, TokenKind.BLOCK_STRING}:
            return "a string"
        return f"{tok.value!r}"

    # -- document ------------------------------------------------------------ #

    def parse_document(self) -> Document:
        definitions: list[Definition] = []
        if self._is(TokenKind.EOF):
            raise self._err("unexpected empty document")
        while not self._is(TokenKind.EOF):
            definitions.append(self._parse_definition())
        return Document(tuple(definitions))

    def _parse_definition(self) -> Definition:
        if self._is(TokenKind.BRACE_L):
            # Anonymous shorthand query.
            return OperationDefinition("query", self._parse_selection_set())
        if self._is(TokenKind.NAME):
            word = self._cur.value
            if word in _OPERATION_KEYWORDS:
                return self._parse_operation()
            if word == "fragment":
                return self._parse_fragment_definition()
        raise self._err(
            f"unexpected {self._describe(self._cur)}; expected an operation or fragment"
        )

    def _parse_operation(self) -> OperationDefinition:
        operation = self._advance().value  # query|mutation|subscription
        name: str | None = None
        if self._is(TokenKind.NAME):
            name = self._advance().value
        var_defs = self._parse_variable_definitions()
        directives = self._parse_directives()
        selection_set = self._parse_selection_set()
        return OperationDefinition(
            operation=operation,
            selection_set=selection_set,
            name=name,
            variable_definitions=var_defs,
            directives=directives,
        )

    def _parse_fragment_definition(self) -> FragmentDefinition:
        self._expect_keyword("fragment")
        name = self._parse_fragment_name()
        self._expect_keyword("on")
        type_condition = self._expect(TokenKind.NAME).value
        directives = self._parse_directives()
        selection_set = self._parse_selection_set()
        return FragmentDefinition(
            name=name,
            type_condition=type_condition,
            selection_set=selection_set,
            directives=directives,
        )

    def _parse_fragment_name(self) -> str:
        tok = self._expect(TokenKind.NAME)
        if tok.value == "on":
            raise GraphQLSyntaxError("fragment name must not be 'on'", tok.line, tok.column)
        return tok.value

    # -- variable definitions ------------------------------------------------ #

    def _parse_variable_definitions(self) -> tuple[VariableDefinition, ...]:
        if not self._is(TokenKind.PAREN_L):
            return ()
        self._advance()
        defs: list[VariableDefinition] = []
        while not self._is(TokenKind.PAREN_R):
            self._expect(TokenKind.DOLLAR)
            name = self._expect(TokenKind.NAME).value
            self._expect(TokenKind.COLON)
            type_ref = self._parse_type_ref()
            default: Value | None = None
            if self._is(TokenKind.EQUALS):
                self._advance()
                default = self._parse_value(is_const=True)
            # variable definition directives are accepted and ignored
            self._parse_directives()
            defs.append(VariableDefinition(name=name, type=type_ref, default_value=default))
        self._expect(TokenKind.PAREN_R)
        return tuple(defs)

    def _parse_type_ref(self) -> TypeRef:
        inner: TypeRef
        if self._is(TokenKind.BRACKET_L):
            self._advance()
            of_type = self._parse_type_ref()
            self._expect(TokenKind.BRACKET_R)
            inner = ListTypeRef(of_type)
        else:
            inner = NamedTypeRef(self._expect(TokenKind.NAME).value)
        if self._is(TokenKind.BANG):
            self._advance()
            return NonNullTypeRef(inner)
        return inner

    # -- selections ---------------------------------------------------------- #

    def _parse_selection_set(self) -> SelectionSet:
        self._expect(TokenKind.BRACE_L)
        selections: list[Selection] = []
        while not self._is(TokenKind.BRACE_R):
            selections.append(self._parse_selection())
        self._expect(TokenKind.BRACE_R)
        if not selections:
            raise self._err("a selection set must not be empty")
        return SelectionSet(tuple(selections))

    def _parse_selection(self) -> Selection:
        if self._is(TokenKind.SPREAD):
            return self._parse_fragment()
        return self._parse_field()

    def _parse_field(self) -> Field:
        tok = self._expect(TokenKind.NAME)
        alias: str | None = None
        name = tok.value
        if self._is(TokenKind.COLON):
            self._advance()
            alias = name
            name = self._expect(TokenKind.NAME).value
        arguments = self._parse_arguments()
        directives = self._parse_directives()
        selection_set: SelectionSet | None = None
        if self._is(TokenKind.BRACE_L):
            selection_set = self._parse_selection_set()
        return Field(
            name=name,
            alias=alias,
            arguments=arguments,
            directives=directives,
            selection_set=selection_set,
            line=tok.line,
            column=tok.column,
        )

    def _parse_fragment(self) -> Selection:
        self._expect(TokenKind.SPREAD)
        if self._is(TokenKind.NAME) and self._cur.value == "on":
            self._advance()
            type_condition = self._expect(TokenKind.NAME).value
            directives = self._parse_directives()
            selection_set = self._parse_selection_set()
            return InlineFragment(type_condition, selection_set, directives)
        if self._is(TokenKind.BRACE_L):
            directives = self._parse_directives()
            return InlineFragment(None, self._parse_selection_set(), directives)
        name = self._parse_fragment_name()
        directives = self._parse_directives()
        return FragmentSpread(name, directives)

    def _parse_arguments(self) -> tuple[Argument, ...]:
        if not self._is(TokenKind.PAREN_L):
            return ()
        self._advance()
        args: list[Argument] = []
        while not self._is(TokenKind.PAREN_R):
            tok = self._expect(TokenKind.NAME)
            self._expect(TokenKind.COLON)
            value = self._parse_value(is_const=False)
            args.append(Argument(tok.value, value, tok.line, tok.column))
        self._expect(TokenKind.PAREN_R)
        return tuple(args)

    def _parse_directives(self) -> tuple[Directive, ...]:
        directives: list[Directive] = []
        while self._is(TokenKind.AT):
            self._advance()
            name = self._expect(TokenKind.NAME).value
            directives.append(Directive(name, self._parse_arguments()))
        return tuple(directives)

    # -- values -------------------------------------------------------------- #

    def _parse_value(self, *, is_const: bool) -> Value:
        tok = self._cur
        kind = tok.kind
        if kind is TokenKind.DOLLAR:
            if is_const:
                raise self._err("unexpected variable in a constant value")
            self._advance()
            return Variable(self._expect(TokenKind.NAME).value)
        if kind is TokenKind.INT:
            self._advance()
            return IntValue(tok.value)
        if kind is TokenKind.FLOAT:
            self._advance()
            return FloatValue(tok.value)
        if kind in {TokenKind.STRING, TokenKind.BLOCK_STRING}:
            self._advance()
            return StringValue(tok.value, block=kind is TokenKind.BLOCK_STRING)
        if kind is TokenKind.BRACKET_L:
            return self._parse_list_value(is_const=is_const)
        if kind is TokenKind.BRACE_L:
            return self._parse_object_value(is_const=is_const)
        if kind is TokenKind.NAME:
            self._advance()
            if tok.value == "true":
                return BooleanValue(True)
            if tok.value == "false":
                return BooleanValue(False)
            if tok.value == "null":
                return NullValue()
            return EnumValue(tok.value)
        raise self._err(f"unexpected {self._describe(tok)} in a value position")

    def _parse_list_value(self, *, is_const: bool) -> ListValue:
        self._expect(TokenKind.BRACKET_L)
        values: list[Value] = []
        while not self._is(TokenKind.BRACKET_R):
            values.append(self._parse_value(is_const=is_const))
        self._expect(TokenKind.BRACKET_R)
        return ListValue(tuple(values))

    def _parse_object_value(self, *, is_const: bool) -> ObjectValue:
        self._expect(TokenKind.BRACE_L)
        fields: list[ObjectField] = []
        seen: set[str] = set()
        while not self._is(TokenKind.BRACE_R):
            name = self._expect(TokenKind.NAME).value
            if name in seen:
                raise self._err(f"duplicate input object field {name!r}")
            seen.add(name)
            self._expect(TokenKind.COLON)
            fields.append(ObjectField(name, self._parse_value(is_const=is_const)))
        self._expect(TokenKind.BRACE_R)
        return ObjectValue(tuple(fields))


def parse(source: str) -> Document:
    """Parse a GraphQL executable document into a :class:`Document` AST."""
    return _Parser(source).parse_document()


__all__ = ["GraphQLSyntaxError", "parse"]
