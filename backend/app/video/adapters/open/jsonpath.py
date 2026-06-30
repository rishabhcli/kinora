"""A tiny, dependency-free JSONPath-ish selector for response extraction.

The provider descriptor (see :mod:`.descriptor`) needs to pull a few scalars out
of an arbitrary provider response: the task id, the status string, the video URL.
Rather than pull in a JSONPath library, this implements the small, well-defined
subset that descriptors actually use — enough to address any nested object/array
shape a real provider returns, and no more:

* ``$`` — the root document.
* ``.key`` / ``key`` — a mapping member (the leading ``$``/``.`` is optional).
* ``[0]`` / ``[-1]`` — a list index (negative indexes count from the end).
* ``[*]`` — *each* element of a list (fans the remaining path over every item,
  flattening the results) — needed for ``output[*].url`` shapes.
* ``a.b || c.d`` — a **fallback chain**: the first expression that resolves to a
  non-null value wins (providers disagree on where the URL lives).

Selection never raises on a missing path — it returns ``None`` — so a descriptor
that's slightly wrong degrades to "no value" instead of crashing a render.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["jsonpath_all", "jsonpath_first", "select"]

#: One path step: a bracketed index/wildcard or a bare/dotted key.
_TOKEN = re.compile(r"\[(-?\d+|\*)\]|([^.\[\]]+)")


def _tokenize(path: str) -> list[str | int]:
    """Split a single path expression into key / index / ``"*"`` steps."""
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    tokens: list[str | int] = []
    for m in _TOKEN.finditer(path):
        bracket, key = m.group(1), m.group(2)
        if bracket is not None:
            tokens.append("*" if bracket == "*" else int(bracket))
        elif key:
            tokens.append(key)
    return tokens


def _walk(node: Any, tokens: list[str | int], i: int, out: list[Any]) -> None:
    if i == len(tokens):
        if node is not None:
            out.append(node)
        return
    token = tokens[i]
    if token == "*":
        if isinstance(node, list):
            for item in node:
                _walk(item, tokens, i + 1, out)
        return
    if isinstance(token, int):
        if isinstance(node, list) and -len(node) <= token < len(node):
            _walk(node[token], tokens, i + 1, out)
        return
    if isinstance(node, dict) and token in node:
        _walk(node[token], tokens, i + 1, out)


def jsonpath_all(doc: Any, path: str) -> list[Any]:
    """Every value addressed by a single (non-fallback) path expression."""
    out: list[Any] = []
    _walk(doc, _tokenize(path), 0, out)
    return out


def select(doc: Any, expr: str) -> Any:
    """Resolve ``expr`` (which may be a ``a || b || c`` fallback chain) to one value.

    Returns the first non-``None`` value across the fallback alternatives, or
    ``None`` when nothing resolves. Never raises on a missing path.
    """
    for alt in expr.split("||"):
        alt = alt.strip()
        if not alt:
            continue
        values = jsonpath_all(doc, alt)
        for value in values:
            if value is not None:
                return value
    return None


def jsonpath_first(doc: Any, path: str) -> Any:
    """The first value addressed by ``path`` (single expression), or ``None``."""
    values = jsonpath_all(doc, path)
    return values[0] if values else None
