"""A hand-rolled GraphQL source tokenizer (a useful subset of the spec grammar).

Covers what executable documents need: punctuators, names, ``Int``/``Float``,
single- and block-string literals (with the standard escape sequences), the
spread ``...`` token, comments, and commas/insignificant whitespace. Source
positions (1-based line/column) are tracked so syntax/validation errors point at
the offending token — the gateway's errors must be useful to API consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TokenKind(StrEnum):
    """The kinds of token the GraphQL lexer emits."""

    BANG = "!"
    DOLLAR = "$"
    AMP = "&"
    PAREN_L = "("
    PAREN_R = ")"
    SPREAD = "..."
    COLON = ":"
    EQUALS = "="
    AT = "@"
    BRACKET_L = "["
    BRACKET_R = "]"
    BRACE_L = "{"
    BRACE_R = "}"
    PIPE = "|"
    NAME = "Name"
    INT = "Int"
    FLOAT = "Float"
    STRING = "String"
    BLOCK_STRING = "BlockString"
    EOF = "<EOF>"


# Single-character punctuators mapped to their token kind.
_PUNCT = {
    "!": TokenKind.BANG,
    "$": TokenKind.DOLLAR,
    "&": TokenKind.AMP,
    "(": TokenKind.PAREN_L,
    ")": TokenKind.PAREN_R,
    ":": TokenKind.COLON,
    "=": TokenKind.EQUALS,
    "@": TokenKind.AT,
    "[": TokenKind.BRACKET_L,
    "]": TokenKind.BRACKET_R,
    "{": TokenKind.BRACE_L,
    "}": TokenKind.BRACE_R,
    "|": TokenKind.PIPE,
}

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical token with its source position."""

    kind: TokenKind
    value: str
    line: int
    column: int

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Token({self.kind.value}, {self.value!r}, {self.line}:{self.column})"


class LexError(Exception):
    """Raised on an invalid token; carries a 1-based source position."""

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column


class Lexer:
    """Stream a GraphQL source string into :class:`Token` values."""

    def __init__(self, source: str) -> None:
        self._src = source
        self._pos = 0
        self._line = 1
        self._line_start = 0  # index of the start of the current line

    @property
    def _column(self) -> int:
        return self._pos - self._line_start + 1

    def _newline(self) -> None:
        self._line += 1
        self._line_start = self._pos

    def tokens(self) -> list[Token]:
        """Tokenize the whole source, ending with an ``EOF`` token."""
        out: list[Token] = []
        while True:
            tok = self.next_token()
            out.append(tok)
            if tok.kind is TokenKind.EOF:
                return out

    def next_token(self) -> Token:
        """Return the next significant token (skipping ignored characters)."""
        self._skip_ignored()
        if self._pos >= len(self._src):
            return Token(TokenKind.EOF, "", self._line, self._column)
        ch = self._src[self._pos]
        line, col = self._line, self._column
        if ch == "." and self._src[self._pos : self._pos + 3] == "...":
            self._pos += 3
            return Token(TokenKind.SPREAD, "...", line, col)
        if ch in _PUNCT:
            self._pos += 1
            return Token(_PUNCT[ch], ch, line, col)
        if ch == '"':
            return self._read_string()
        if ch == "-" or ch.isdigit():
            return self._read_number()
        if ch == "_" or ch.isalpha():
            return self._read_name()
        raise LexError(f"Unexpected character {ch!r}", line, col)

    def _skip_ignored(self) -> None:
        src = self._src
        n = len(src)
        while self._pos < n:
            ch = src[self._pos]
            if ch == "\n":
                self._pos += 1
                self._newline()
            elif ch == "\r":
                self._pos += 1
                if self._pos < n and src[self._pos] == "\n":
                    self._pos += 1
                self._newline()
            elif ch in " \t﻿,":
                self._pos += 1
            elif ch == "#":
                while self._pos < n and src[self._pos] not in "\r\n":
                    self._pos += 1
            else:
                break

    def _read_name(self) -> Token:
        src = self._src
        start = self._pos
        line, col = self._line, self._column
        n = len(src)
        while self._pos < n and (src[self._pos] == "_" or src[self._pos].isalnum()):
            self._pos += 1
        return Token(TokenKind.NAME, src[start : self._pos], line, col)

    def _read_number(self) -> Token:
        src = self._src
        start = self._pos
        line, col = self._line, self._column
        n = len(src)
        is_float = False
        if src[self._pos] == "-":
            self._pos += 1
        # Integer part
        if self._pos < n and src[self._pos] == "0":
            self._pos += 1
        else:
            self._consume_digits(line, col)
        # Fractional part
        if self._pos < n and src[self._pos] == ".":
            is_float = True
            self._pos += 1
            self._consume_digits(line, col)
        # Exponent
        if self._pos < n and src[self._pos] in "eE":
            is_float = True
            self._pos += 1
            if self._pos < n and src[self._pos] in "+-":
                self._pos += 1
            self._consume_digits(line, col)
        raw = src[start : self._pos]
        return Token(TokenKind.FLOAT if is_float else TokenKind.INT, raw, line, col)

    def _consume_digits(self, line: int, col: int) -> None:
        src = self._src
        n = len(src)
        if self._pos >= n or not src[self._pos].isdigit():
            raise LexError("Invalid number, expected digit", line, col)
        while self._pos < n and src[self._pos].isdigit():
            self._pos += 1

    def _read_string(self) -> Token:
        src = self._src
        line, col = self._line, self._column
        if src[self._pos : self._pos + 3] == '"""':
            return self._read_block_string(line, col)
        self._pos += 1  # opening quote
        chars: list[str] = []
        n = len(src)
        while self._pos < n:
            ch = src[self._pos]
            if ch == '"':
                self._pos += 1
                return Token(TokenKind.STRING, "".join(chars), line, col)
            if ch in "\r\n":
                raise LexError("Unterminated string", line, col)
            if ch == "\\":
                self._pos += 1
                if self._pos >= n:
                    raise LexError("Unterminated string escape", line, col)
                esc = src[self._pos]
                if esc in _ESCAPES:
                    chars.append(_ESCAPES[esc])
                    self._pos += 1
                elif esc == "u":
                    hexd = src[self._pos + 1 : self._pos + 5]
                    if len(hexd) != 4 or any(c not in "0123456789abcdefABCDEF" for c in hexd):
                        raise LexError("Invalid unicode escape", line, col)
                    chars.append(chr(int(hexd, 16)))
                    self._pos += 5
                else:
                    raise LexError(f"Invalid escape \\{esc}", line, col)
            else:
                chars.append(ch)
                self._pos += 1
        raise LexError("Unterminated string", line, col)

    def _read_block_string(self, line: int, col: int) -> Token:
        src = self._src
        self._pos += 3  # opening """
        chars: list[str] = []
        n = len(src)
        while self._pos < n:
            if src[self._pos : self._pos + 3] == '"""':
                self._pos += 3
                return Token(
                    TokenKind.BLOCK_STRING, _dedent_block("".join(chars)), line, col
                )
            ch = src[self._pos]
            if ch == "\\" and src[self._pos : self._pos + 4] == '\\"""':
                chars.append('"""')
                self._pos += 4
                continue
            if ch == "\n":
                self._newline()
            elif ch == "\r":
                if src[self._pos + 1 : self._pos + 2] == "\n":
                    self._pos += 1
                self._newline()
                chars.append("\n")
                self._pos += 1
                continue
            chars.append(ch)
            self._pos += 1
        raise LexError("Unterminated block string", line, col)


def _dedent_block(raw: str) -> str:
    """Apply the GraphQL block-string value algorithm (common-indent strip)."""
    lines = raw.split("\n")
    common: int | None = None
    for line in lines[1:]:
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if stripped:
            common = indent if common is None else min(common, indent)
    if common:
        lines = [lines[0]] + [ln[common:] for ln in lines[1:]]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


__all__ = ["LexError", "Lexer", "Token", "TokenKind"]
