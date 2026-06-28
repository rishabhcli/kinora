"""Unit tests for the GraphQL language layer (lexer + parser + printer).

Pure, infra-free: they exercise the hand-rolled tokenizer, recursive-descent
parser, and AST printer directly.
"""

from __future__ import annotations

import pytest

from app.graphql.language import parse, print_ast
from app.graphql.language.ast import (
    Field,
    FragmentDefinition,
    InlineFragment,
    OperationDefinition,
)
from app.graphql.language.lexer import Lexer, TokenKind
from app.graphql.language.parser import GraphQLSyntaxError


def _kinds(source: str) -> list[TokenKind]:
    return [t.kind for t in Lexer(source).tokens()]


def test_lexer_tokenizes_punctuators_and_names() -> None:
    kinds = _kinds("{ book(id: \"x\") { id } }")
    assert kinds[0] is TokenKind.BRACE_L
    assert TokenKind.NAME in kinds
    assert TokenKind.STRING in kinds
    assert kinds[-1] is TokenKind.EOF


def test_lexer_spread_and_numbers() -> None:
    kinds = _kinds("... 12 -3 4.5 1e3 -2.0e-1")
    assert kinds[0] is TokenKind.SPREAD
    assert kinds[1] is TokenKind.INT
    assert kinds[2] is TokenKind.INT
    assert kinds[3] is TokenKind.FLOAT
    assert kinds[4] is TokenKind.FLOAT
    assert kinds[5] is TokenKind.FLOAT


def test_lexer_string_escapes() -> None:
    toks = Lexer(r'"a\nb\tcA"').tokens()
    assert toks[0].kind is TokenKind.STRING
    assert toks[0].value == "a\nb\tcA"


def test_lexer_block_string_dedent() -> None:
    src = '"""\n    hello\n    world\n    """'
    toks = Lexer(src).tokens()
    assert toks[0].kind is TokenKind.BLOCK_STRING
    assert toks[0].value == "hello\nworld"


def test_lexer_skips_comments_and_commas() -> None:
    kinds = _kinds("# a comment\n{ a, b }")
    assert kinds == [
        TokenKind.BRACE_L,
        TokenKind.NAME,
        TokenKind.NAME,
        TokenKind.BRACE_R,
        TokenKind.EOF,
    ]


def test_parse_shorthand_query() -> None:
    doc = parse("{ book(id: \"abc\") { id title } }")
    ops = doc.operations()
    assert len(ops) == 1
    op = ops[0]
    assert op.operation == "query"
    assert op.name is None
    field = op.selection_set.selections[0]
    assert isinstance(field, Field)
    assert field.name == "book"
    assert field.arguments[0].name == "id"


def test_parse_named_operation_with_variables() -> None:
    doc = parse(
        "query GetBook($id: ID!, $n: Int = 5) { book(id: $id) { shots(first: $n) { "
        "edges { node { id } } } } }"
    )
    op = doc.operations()[0]
    assert isinstance(op, OperationDefinition)
    assert op.name == "GetBook"
    assert len(op.variable_definitions) == 2
    assert op.variable_definitions[1].default_value is not None


def test_parse_mutation() -> None:
    doc = parse('mutation { createReadingSession(input: {bookId: "b1"}) { id } }')
    assert doc.operations()[0].operation == "mutation"


def test_parse_fragments_named_and_inline() -> None:
    doc = parse(
        """
        query { node(id: "x") { ... on Book { id } ...bookFields } }
        fragment bookFields on Book { title author }
        """
    )
    frags = doc.fragments()
    assert "bookFields" in frags
    assert isinstance(frags["bookFields"], FragmentDefinition)
    node_field = doc.operations()[0].selection_set.selections[0]
    assert isinstance(node_field, Field)
    inline = node_field.selection_set.selections[0]
    assert isinstance(inline, InlineFragment)
    assert inline.type_condition == "Book"


def test_parse_aliases_and_directives() -> None:
    doc = parse("{ a: book(id: \"1\") @include(if: true) { id } }")
    field = doc.operations()[0].selection_set.selections[0]
    assert isinstance(field, Field)
    assert field.alias == "a"
    assert field.response_key == "a"
    assert field.directives[0].name == "include"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "{ }",
        "query {",
        "{ a(",
        "fragment on Book { id }",  # missing name
        "type Book { id }",  # SDL not allowed
        "{ a: }",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(GraphQLSyntaxError):
        parse(bad)


def test_printer_round_trips() -> None:
    src = 'query Q($id: ID!) { book(id: $id) { id title } }'
    doc = parse(src)
    printed = print_ast(doc)
    # Re-parse the printed output and confirm the operation survives.
    reparsed = parse(printed)
    assert reparsed.operations()[0].name == "Q"
    assert "book(id: $id)" in printed


def test_printer_handles_fragments_and_lists() -> None:
    src = '{ books(status: READY) { edges { node { id } } } }'
    printed = print_ast(parse(src))
    assert "READY" in printed
    assert "edges" in printed
