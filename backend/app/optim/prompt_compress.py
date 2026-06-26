"""Pure helpers that cut input tokens before a DashScope call — opt-in, behavior-preserving.

Nothing here is applied automatically; an agent *chooses* to shrink what it sends. The wins:

* :func:`collapse_whitespace` — fold whitespace runs (cheap token savings, no semantic change).
* :func:`dedupe_canon` — drop re-sent canon blocks (the same fact often arrives N times in a slice).
* :func:`trim_messages_to_budget` — keep system + the most recent window under a token budget.
* :func:`compact_json_schema` — strip schema chrome (``description``/``title``/``examples``/…)
  before a structured-output call; schema-aware, so it never deletes a *property* named that.
* :func:`estimate_tokens` / :func:`compression_ratio` — a deterministic estimator + savings
  fraction for before/after measurement (see coordination/PERF.md). The estimator is the
  ~4-chars/token heuristic — model-exact counts differ, but it is stable, so *ratios* are valid.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: Chars per token for the rough estimator (English-ish; stable for relative comparison).
_CHARS_PER_TOKEN = 4
_WS_RUN = re.compile(r"\s+")

#: JSON-Schema keys that are pure chrome for an LLM (carry no constraint).
_SCHEMA_CHROME = frozenset(
    {"description", "title", "examples", "example", "default", "$comment", "readOnly"}
)
#: Schema keys whose *values* are dicts keyed by NAME (never treat those keys as chrome).
_NAMED_CONTAINERS = frozenset({"properties", "$defs", "definitions", "patternProperties"})


def estimate_tokens(text: str) -> int:
    """Rough token count via the ~4-chars/token heuristic (``ceil(len/4)``; empty ⇒ 0)."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN) if text else 0


def collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends."""
    return _WS_RUN.sub(" ", text).strip()


def dedupe_canon(blocks: Sequence[str]) -> list[str]:
    """Drop duplicate canon blocks; keep the first verbatim, in order.

    Compared after whitespace normalization, so spacing-only re-sends still dedupe.
    """
    seen: set[str] = set()
    out: list[str] = []
    for block in blocks:
        key = collapse_whitespace(block)
        if key not in seen:
            seen.add(key)
            out.append(block)
    return out


def trim_messages_to_budget(
    messages: Sequence[Mapping[str, Any]],
    budget_tokens: int,
    *,
    estimator: Callable[[str], int] = estimate_tokens,
) -> list[dict[str, Any]]:
    """Keep all ``system`` messages + the most-recent window that fits ``budget_tokens``.

    System messages are never dropped (even if they alone exceed the budget). The returned list is a
    subset of ``messages`` in the **original order**. Pure: inputs are not mutated.
    """

    def toks(msg: Mapping[str, Any]) -> int:
        content = msg.get("content", "")
        return estimator(content if isinstance(content, str) else str(content))

    keep = [False] * len(messages)
    used = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            keep[i] = True
            used += toks(msg)
    # Walk newest → oldest, including non-system messages until the next one would overflow.
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "system":
            continue
        cost = toks(msg)
        if used + cost > budget_tokens:
            break
        used += cost
        keep[i] = True
    return [dict(messages[i]) for i in range(len(messages)) if keep[i]]


def compact_json_schema(node: Any) -> Any:
    """Return ``node`` with schema chrome stripped recursively (pure; original untouched).

    Schema-aware: under a named container (``properties``/``$defs``/…) the keys are
    property/definition *names* and are preserved — only their sub-schemas are compacted.
    """
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _SCHEMA_CHROME:
                continue
            if key in _NAMED_CONTAINERS and isinstance(value, Mapping):
                out[key] = {name: compact_json_schema(sub) for name, sub in value.items()}
            else:
                out[key] = compact_json_schema(value)
        return out
    if isinstance(node, list):
        return [compact_json_schema(item) for item in node]
    return node


def compression_ratio(before: int, after: int) -> float:
    """Fraction saved, in ``[0, 1]``: ``(before-after)/before``. Zero when ``before <= 0``."""
    if before <= 0:
        return 0.0
    return max(0.0, (before - after) / before)


__all__ = [
    "collapse_whitespace",
    "compact_json_schema",
    "compression_ratio",
    "dedupe_canon",
    "estimate_tokens",
    "trim_messages_to_budget",
]
