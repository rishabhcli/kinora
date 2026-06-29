"""Vocabulary-agnostic token utilities.

The accel algorithms treat a "token" as an opaque string. These helpers give a
deterministic word tokenizer (used by the test backends and as a sensible
default when the wrapped backend only hands us text) plus the longest-common-
prefix primitive that both speculative accept and prefix reuse rely on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_WORD_RE = re.compile(r"\S+")


def word_tokens(text: str) -> tuple[str, ...]:
    """Split ``text`` into whitespace-delimited word tokens (order-preserving)."""
    return tuple(_WORD_RE.findall(text))


def join_tokens(tokens: Sequence[str], joiner: str = " ") -> str:
    """Render tokens back to text (inverse of :func:`word_tokens` for words)."""
    return joiner.join(tokens)


def common_prefix_length(a: Sequence[str], b: Sequence[str]) -> int:
    """Length of the longest shared leading run of ``a`` and ``b``."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def common_prefix(a: Sequence[str], b: Sequence[str]) -> tuple[str, ...]:
    """The longest shared leading run, as a tuple."""
    return tuple(a[: common_prefix_length(a, b)])


__all__ = ["common_prefix", "common_prefix_length", "join_tokens", "word_tokens"]
