"""A length-aware prompt compressor — shrink a rendered prompt to a budget.

Each video model caps its prompt length (Pika and Kling are famously short; Veo
and Sora take long paragraphs). A dialect renders a *list of clauses* in priority
order (most important first) and hands them here with a character budget; the
compressor keeps as many leading clauses as fit, then — if even the first clause
overflows — hard-truncates it on a word boundary so the result is never empty and
never over budget.

Two layers:

* :func:`fit_clauses` — drop trailing (lowest-priority) clauses until the joined
  string fits, then word-truncate the survivor if a single clause still overflows.
* :func:`shorten_text` — the last-resort single-string word-boundary truncation.

Pure, deterministic, no I/O. Budgets are in characters (a stable, model-neutral
proxy; the optim layer's token estimator is ~4 chars/token if a token budget is
preferred — callers convert).
"""

from __future__ import annotations

from collections.abc import Sequence

#: Default joiner between clauses (matches the generator's ". " sentence style).
DEFAULT_SEPARATOR = ". "
#: Appended when a single clause must be hard-truncated, signalling the cut.
_ELLIPSIS = "…"


def shorten_text(text: str, budget: int, *, ellipsis: str = _ELLIPSIS) -> str:
    """Truncate ``text`` to at most ``budget`` characters on a word boundary.

    The ellipsis (if it fits) is included *within* the budget. A non-positive
    budget yields ``""``. When no word boundary fits, falls back to a hard
    character cut so the result still respects the budget. Pure.
    """
    text = text.strip()
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    keep = budget - len(ellipsis)
    if keep <= 0:
        # No room even for the ellipsis — hard char cut, no marker.
        return text[:budget].rstrip()
    head = text[:keep]
    # Prefer the last whitespace so we never split a word; if the clause is one
    # long token, fall back to the hard slice.
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return f"{head.rstrip()}{ellipsis}"


def join_within(clauses: Sequence[str], budget: int, *, separator: str = DEFAULT_SEPARATOR) -> str:
    """Join ``clauses`` with ``separator``, keeping every clause that fits the budget.

    Walks clauses in order (priority order is the caller's responsibility) and
    appends each whose addition keeps the running length within ``budget``;
    lower-priority clauses that would overflow are skipped (not just the tail —
    a short trailing clause can still be admitted after a long one was dropped).
    Returns ``""`` if nothing fits. Pure.
    """
    kept: list[str] = []
    used = 0
    sep_len = len(separator)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        addition = len(clause) + (sep_len if kept else 0)
        if used + addition <= budget:
            kept.append(clause)
            used += addition
    return separator.join(kept)


def fit_clauses(
    clauses: Sequence[str],
    budget: int,
    *,
    separator: str = DEFAULT_SEPARATOR,
    ellipsis: str = _ELLIPSIS,
) -> str:
    """Fit priority-ordered ``clauses`` into ``budget`` characters (never empty if input isn't).

    Strategy:

    1. Keep the longest leading prefix of clauses that fits (drop the tail).
    2. If *no* whole clause fits (even the first overflows), word-truncate the
       first clause to the budget so a non-empty prompt is still produced.

    The result is guaranteed ``<= budget`` characters and is empty only when every
    input clause is blank or the budget is non-positive. Pure.
    """
    cleaned = [c.strip() for c in clauses if c and c.strip()]
    if not cleaned or budget <= 0:
        return ""
    # Greedily keep a leading prefix (priority order: never reorder, only drop tail).
    kept: list[str] = []
    used = 0
    sep_len = len(separator)
    for clause in cleaned:
        addition = len(clause) + (sep_len if kept else 0)
        if used + addition <= budget:
            kept.append(clause)
            used += addition
        else:
            break
    if kept:
        return separator.join(kept)
    # Even the first clause overflows — truncate it rather than return nothing.
    return shorten_text(cleaned[0], budget, ellipsis=ellipsis)


__all__ = [
    "DEFAULT_SEPARATOR",
    "fit_clauses",
    "join_within",
    "shorten_text",
]
