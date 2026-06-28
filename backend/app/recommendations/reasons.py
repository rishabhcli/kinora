"""Explainable "because you read X" reason synthesis.

Every recommendation carries a tuple of :class:`~.types.Reason` objects — the
signed per-signal contributions that put it on the list. This module turns those
structured reasons into a single human-readable line for the UI, choosing the
*most impactful* reason as the headline and keeping the phrasing concrete
("Because you read *The Snow Queen*") rather than opaque ("high relevance").

Pure string synthesis — no model call. The recsys is explainable by
construction: the reason objects come straight from which candidate-source fired
and with what weight, so the explanation can never drift from the actual score.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .types import BookFeatures, Reason, ReasonKind


def _title_for(book_id: str | None, features: Mapping[str, BookFeatures] | None) -> str | None:
    if not book_id:
        return None
    if features and book_id in features and features[book_id].title:
        return features[book_id].title
    return None


def dominant_reason(reasons: Sequence[Reason]) -> Reason | None:
    """The single reason with the largest absolute contribution (the headline)."""
    positive = [r for r in reasons if r.contribution > 0.0]
    pool = positive or list(reasons)
    if not pool:
        return None
    return max(pool, key=lambda r: abs(r.contribution))


def explain(
    reasons: Sequence[Reason],
    *,
    features: Mapping[str, BookFeatures] | None = None,
) -> str:
    """One-line natural-language explanation for a recommendation.

    Picks the dominant reason and phrases it. Content/collaborative reasons name
    the seed book ("Because you read X" / "Readers who enjoyed X also watched
    this"); taste/popularity/boost reasons phrase the signal. A seed title falls
    back to the reason's own ``seed_title`` then a generic phrase when unknown.
    """
    top = dominant_reason(reasons)
    if top is None:
        return "Recommended for you"

    seed_title = top.seed_title or _title_for(top.seed_book_id, features)
    if top.kind is ReasonKind.CONTENT:
        if seed_title:
            return f"Because you read {seed_title}"
        return "Similar to books you've read"
    if top.kind is ReasonKind.COLLABORATIVE:
        if seed_title:
            return f"Readers who enjoyed {seed_title} also watched this"
        return "Popular with readers like you"
    if top.kind is ReasonKind.TASTE:
        return "Matches your reading taste"
    if top.kind is ReasonKind.POPULAR:
        return "Trending on Kinora"
    if top.kind is ReasonKind.BOOST:
        return top.detail or "Featured for you"
    return "Recommended for you"


def summarize(
    reasons: Sequence[Reason],
    *,
    features: Mapping[str, BookFeatures] | None = None,
    limit: int = 3,
) -> list[str]:
    """Up to ``limit`` short reason phrases, impact-ordered (for a "why" tooltip)."""
    ranked = sorted(
        (r for r in reasons if r.contribution > 0.0),
        key=lambda r: r.contribution,
        reverse=True,
    )
    out: list[str] = []
    seen: set[str] = set()
    for reason in ranked:
        phrase = explain([reason], features=features)
        if phrase not in seen:
            seen.add(phrase)
            out.append(phrase)
        if len(out) >= limit:
            break
    return out


def order_reasons(reasons: Iterable[Reason]) -> tuple[Reason, ...]:
    """Reasons sorted by descending absolute contribution (most impactful first)."""
    return tuple(sorted(reasons, key=lambda r: abs(r.contribution), reverse=True))


__all__ = [
    "dominant_reason",
    "explain",
    "order_reasons",
    "summarize",
]
