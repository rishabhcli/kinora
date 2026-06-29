"""A tiny, safe arithmetic interpreter for derived-metric expressions.

Derived metrics carry an expression like ``(1 - rejected / total) * 100`` over
named input metrics. We must *not* use Python ``eval`` (arbitrary code), so this
module ships a minimal recursive-descent parser + evaluator supporting:

* the four binary operators ``+ - * /`` with correct precedence + parentheses;
* unary minus;
* non-negative numeric literals (int / float);
* bare identifiers, resolved through a supplied ``{name: value}`` environment.

Division by zero yields ``0.0`` rather than raising — a metrics layer reports a
zero ratio for an empty denominator (matching :func:`accepted_footage_efficiency`
and the §13 KPI conventions) instead of blowing up a dashboard. ``None`` inputs
(an empty aggregate group) propagate as ``None`` through the whole expression.

The parser is compiled once per expression via :func:`compile_expr` and the
resulting AST is evaluated per row — cheap and side-effect-free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Tokeniser
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+\.\d+|\d+\.|\.\d+|\d+)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>[()+\-*/]))"
)


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # "num" | "ident" | "op"
    text: str


def _tokenize(expr: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(expr, pos)
        if not match or match.end() == pos:
            raise ValueError(f"unexpected character {expr[pos]!r} in expression {expr!r}")
        pos = match.end()
        if match.lastgroup == "num":
            tokens.append(_Token("num", match.group("num")))
        elif match.lastgroup == "ident":
            tokens.append(_Token("ident", match.group("ident")))
        else:
            tokens.append(_Token("op", match.group("op")))
    return tokens


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Num:
    value: float


@dataclass(frozen=True, slots=True)
class Var:
    name: str


@dataclass(frozen=True, slots=True)
class BinOp:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class Neg:
    operand: Expr


Expr = Num | Var | BinOp | Neg


# --------------------------------------------------------------------------- #
# Recursive-descent parser (precedence: + - below * /)
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self._tokens = tokens
        self._i = 0

    def parse(self) -> Expr:
        node = self._expr()
        if self._i != len(self._tokens):
            raise ValueError("trailing tokens in expression")
        return node

    def _peek(self) -> _Token | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _advance(self) -> _Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _expr(self) -> Expr:
        node = self._term()
        while (tok := self._peek()) and tok.kind == "op" and tok.text in "+-":
            self._advance()
            node = BinOp(tok.text, node, self._term())
        return node

    def _term(self) -> Expr:
        node = self._factor()
        while (tok := self._peek()) and tok.kind == "op" and tok.text in "*/":
            self._advance()
            node = BinOp(tok.text, node, self._factor())
        return node

    def _factor(self) -> Expr:
        tok = self._peek()
        if tok is None:
            raise ValueError("unexpected end of expression")
        if tok.kind == "op" and tok.text == "-":
            self._advance()
            return Neg(self._factor())
        if tok.kind == "op" and tok.text == "(":
            self._advance()
            node = self._expr()
            close = self._peek()
            if close is None or close.text != ")":
                raise ValueError("missing closing parenthesis")
            self._advance()
            return node
        if tok.kind == "num":
            self._advance()
            return Num(float(tok.text))
        if tok.kind == "ident":
            self._advance()
            return Var(tok.text)
        raise ValueError(f"unexpected token {tok.text!r}")


def compile_expr(expr: str) -> Expr:
    """Parse a derived-metric expression into an evaluable AST (raises on garbage)."""
    return _Parser(_tokenize(expr)).parse()


def referenced_names(node: Expr) -> frozenset[str]:
    """Identifiers used anywhere in a compiled expression (for validation)."""
    if isinstance(node, Var):
        return frozenset({node.name})
    if isinstance(node, Num):
        return frozenset()
    if isinstance(node, Neg):
        return referenced_names(node.operand)
    return referenced_names(node.left) | referenced_names(node.right)


def evaluate(node: Expr, env: Mapping[str, float | None]) -> float | None:
    """Evaluate a compiled expression against ``env`` (``None`` propagates; /0 -> 0)."""
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Var):
        if node.name not in env:
            raise KeyError(f"derived expression references unbound name {node.name!r}")
        return env[node.name]
    if isinstance(node, Neg):
        val = evaluate(node.operand, env)
        return None if val is None else -val
    left = evaluate(node.left, env)
    right = evaluate(node.right, env)
    if left is None or right is None:
        return None
    if node.op == "+":
        return left + right
    if node.op == "-":
        return left - right
    if node.op == "*":
        return left * right
    # division: empty denominator -> 0.0 (metrics convention, no ZeroDivisionError)
    return left / right if right != 0 else 0.0


__all__ = ["Expr", "compile_expr", "evaluate", "referenced_names"]
