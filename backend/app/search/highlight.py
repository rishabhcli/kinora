"""Snippet extraction + term highlighting for search results.

Given a matched field's original text and the query's analyzed terms, produce a
short *snippet* (the best window of text around the densest cluster of matches)
with the matched terms wrapped in highlight tags (``<mark>…</mark>`` by default).

Highlighting runs on the *original* text (not the analyzed terms) so the snippet
reads naturally, but matching is done against analyzed terms so a search for
"running" highlights the word "ran" / "runs" when they stem together. This means
the highlighter re-analyzes each candidate token at the same stemming setting as
the index — keeping index/query/highlight analysis consistent.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from app.search.analyzer import Analyzer, default_analyzer

_WORD_SPAN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Snippet:
    """A highlighted snippet: the marked-up fragment + whether it was truncated."""

    text: str
    truncated: bool


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    surface: str
    term: str


def _tokenize_spans(text: str, analyzer: Analyzer) -> list[_Token]:
    """Tokens with their character spans + analyzed term (for offset-true marking)."""
    out: list[_Token] = []
    for m in _WORD_SPAN_RE.finditer(text):
        surface = m.group(0)
        folded = surface.lower()
        term = folded if analyzer.is_stop(folded) else analyzer.normalize_token(folded)
        out.append(_Token(start=m.start(), end=m.end(), surface=surface, term=term))
    return out


def highlight(
    text: str,
    query_terms: set[str],
    *,
    analyzer: Analyzer | None = None,
    pre_tag: str = "<mark>",
    post_tag: str = "</mark>",
    max_chars: int = 240,
    escape: bool = True,
) -> Snippet:
    """Return the best snippet of ``text`` with ``query_terms`` highlighted.

    ``query_terms`` are *analyzed* terms (already stemmed/synonym-expanded by the
    caller — i.e. the same pipeline the index used). The window is centred on the
    densest cluster of matches and clipped to ``max_chars``; when ``escape`` is
    set the surrounding text is HTML-escaped so the snippet is XSS-safe to render
    (the highlight tags are added *after* escaping).
    """
    analyzer = analyzer or default_analyzer()
    if not text:
        return Snippet(text="", truncated=False)
    tokens = _tokenize_spans(text, analyzer)
    match_idx = [i for i, t in enumerate(tokens) if t.term in query_terms]

    if not match_idx:
        fragment, truncated = _head(text, max_chars)
        return Snippet(text=html.escape(fragment) if escape else fragment, truncated=truncated)

    lo_char, hi_char = _best_window(text, tokens, match_idx, max_chars)
    fragment = text[lo_char:hi_char]
    truncated = lo_char > 0 or hi_char < len(text)

    # Mark matches that fall inside the window, working right-to-left so earlier
    # spans' offsets stay valid as we splice in tags.
    pieces: list[str] = []
    cursor = lo_char
    for i in match_idx:
        tok = tokens[i]
        if tok.start < lo_char or tok.end > hi_char:
            continue
        before = text[cursor : tok.start]
        word = text[tok.start : tok.end]
        if escape:
            before = html.escape(before)
            word = html.escape(word)
        pieces.append(before)
        pieces.append(f"{pre_tag}{word}{post_tag}")
        cursor = tok.end
    tail = text[cursor:hi_char]
    pieces.append(html.escape(tail) if escape else tail)

    out = "".join(pieces)
    if lo_char > 0:
        out = "…" + out
    if hi_char < len(text):
        out = out + "…"
    return Snippet(text=out, truncated=truncated)


def _head(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind(" ", 0, max_chars)
    cut = cut if cut > max_chars // 2 else max_chars
    return text[:cut], True


def _best_window(
    text: str, tokens: list[_Token], match_idx: list[int], max_chars: int
) -> tuple[int, int]:
    """Pick the character window covering the densest cluster of matches.

    Slides a window over the matched token positions and keeps the one that
    covers the most matches within ``max_chars``; ties break toward the earliest
    window so the snippet reads from the start of the passage.
    """
    best_lo = 0
    best_count = -1
    n = len(match_idx)
    for s in range(n):
        anchor = tokens[match_idx[s]]
        win_lo = anchor.start
        win_hi = min(len(text), win_lo + max_chars)
        count = 0
        for k in range(s, n):
            tok = tokens[match_idx[k]]
            if tok.end <= win_hi:
                count += 1
            else:
                break
        if count > best_count:
            best_count = count
            best_lo = win_lo

    # Expand to whole-word boundaries and add a little left context.
    lead = max_chars // 4
    lo = max(0, best_lo - lead)
    lo = _snap_left(text, lo)
    hi = min(len(text), lo + max_chars)
    hi = _snap_right(text, hi)
    return lo, hi


def _snap_left(text: str, pos: int) -> int:
    if pos <= 0:
        return 0
    space = text.rfind(" ", 0, pos)
    return space + 1 if space != -1 else pos


def _snap_right(text: str, pos: int) -> int:
    if pos >= len(text):
        return len(text)
    space = text.find(" ", pos)
    return space if space != -1 else pos


def analyze_query_terms(query_text: str, analyzer: Analyzer | None = None) -> set[str]:
    """Analyze a query string into the set of terms the highlighter marks on."""
    analyzer = analyzer or default_analyzer()
    return set(analyzer.analyze(query_text))


__all__ = ["Snippet", "analyze_query_terms", "highlight"]
