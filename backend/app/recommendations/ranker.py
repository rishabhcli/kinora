"""The scoring + re-ranking stage: blend the signals, diversify, explain.

This is where the recall sources converge. Each :class:`~.types.Candidate`
arrives carrying raw per-signal scores (content / collaborative / taste /
popular). The ranker:

1. **Blends** them into one score via :class:`~.types.BlendWeights` and records a
   signed :class:`~.types.Reason` per contributing signal (explainability is a
   byproduct of scoring, not bolted on).
2. Applies **business-rule boosts** — additive, pluggable rules (same author,
   shares a tag with a loved book, brand-new release) that nudge a candidate up
   without distorting the learned signals.
3. **Re-ranks for diversity** with Maximal Marginal Relevance, reusing
   ``app.memory.retrieval.mmr_rerank`` over the candidates' content vectors so
   the final list isn't ten near-identical sequels — the same diversity
   discipline the canon slice uses (§8.4).
4. **Dedups** and assigns 1-based ranks + a natural-language explanation.

Pure given the inputs; tests pin the blend arithmetic and the MMR ordering.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.memory.retrieval import Scored, mmr_rerank

from . import reasons as reason_synth
from .types import (
    BlendWeights,
    BookFeatures,
    Candidate,
    Reason,
    ReasonKind,
    Recommendation,
    ScoredCandidate,
)

#: A business rule: given a candidate book and the user context, return an
#: additive boost (>= 0) plus a human-readable detail, or ``None`` to abstain.
BoostRule = Callable[["BoostContext"], "tuple[float, str] | None"]


@dataclass(frozen=True, slots=True)
class BoostContext:
    """What a :data:`BoostRule` sees: the candidate, the corpus, the user's books."""

    book: BookFeatures
    features: Mapping[str, BookFeatures]
    engaged_book_ids: frozenset[str]
    #: Loved books (strong positive engagement) — the seeds for affinity rules.
    loved_book_ids: frozenset[str]


def same_author_boost(amount: float = 0.15) -> BoostRule:
    """Boost a candidate written by an author the reader has loved."""

    def rule(ctx: BoostContext) -> tuple[float, str] | None:
        if not ctx.book.author:
            return None
        loved_authors = {
            ctx.features[b].author
            for b in ctx.loved_book_ids
            if b in ctx.features and ctx.features[b].author
        }
        if ctx.book.author in loved_authors:
            return amount, f"More from {ctx.book.author}"
        return None

    return rule


def shared_tag_boost(amount: float = 0.1) -> BoostRule:
    """Boost a candidate that shares a tag with a book the reader loved."""

    def rule(ctx: BoostContext) -> tuple[float, str] | None:
        if not ctx.book.tags:
            return None
        loved_tags: set[str] = set()
        for b in ctx.loved_book_ids:
            if b in ctx.features:
                loved_tags |= set(ctx.features[b].tags)
        shared = set(ctx.book.tags) & loved_tags
        if shared:
            return amount, f"More {sorted(shared)[0]}"
        return None

    return rule


def blend_candidate(
    candidate: Candidate,
    weights: BlendWeights,
    *,
    features: Mapping[str, BookFeatures] | None = None,
) -> ScoredCandidate:
    """Blend a candidate's per-signal scores into one score + signed reasons.

    Each signal present in ``candidate.source_scores`` contributes
    ``weight · raw_score`` and emits a :class:`Reason` with that contribution and
    the signal's seed book (when one was attributed). Reasons are returned
    impact-ordered so the dominant one is the headline.
    """
    weight_map = weights.as_map()
    total = 0.0
    reasons: list[Reason] = []
    for kind, raw in candidate.source_scores.items():
        w = weight_map.get(kind, 0.0)
        contribution = w * raw
        if contribution == 0.0:
            continue
        total += contribution
        seed_id, seed_title = None, None
        if kind in candidate.seeds:
            seed_id = candidate.seeds[kind][0]
            if features and seed_id in features:
                seed_title = features[seed_id].title or None
        reasons.append(
            Reason(
                kind=kind,
                contribution=contribution,
                seed_book_id=seed_id,
                seed_title=seed_title,
            )
        )
    vector = (
        features[candidate.book_id].embedding if features and candidate.book_id in features else []
    )
    return ScoredCandidate(
        book_id=candidate.book_id,
        score=total,
        reasons=reason_synth.order_reasons(reasons),
        vector=list(vector),
    )


def apply_boosts(
    scored: ScoredCandidate,
    rules: Sequence[BoostRule],
    ctx: BoostContext,
) -> ScoredCandidate:
    """Add business-rule boosts to a scored candidate (additive, explainable)."""
    if not rules:
        return scored
    total_boost = 0.0
    boost_reasons: list[Reason] = []
    for rule in rules:
        result = rule(ctx)
        if result is None:
            continue
        amount, detail = result
        if amount == 0.0:
            continue
        total_boost += amount
        boost_reasons.append(Reason(kind=ReasonKind.BOOST, contribution=amount, detail=detail))
    if total_boost == 0.0:
        return scored
    return ScoredCandidate(
        book_id=scored.book_id,
        score=scored.score + total_boost,
        reasons=reason_synth.order_reasons([*scored.reasons, *boost_reasons]),
        vector=scored.vector,
    )


def score_candidates(
    candidates: Iterable[Candidate],
    weights: BlendWeights,
    *,
    features: Mapping[str, BookFeatures] | None = None,
    boost_rules: Sequence[BoostRule] = (),
    boost_ctx: Callable[[str], BoostContext] | None = None,
) -> list[ScoredCandidate]:
    """Blend + boost every candidate, returning them sorted by descending score."""
    out: list[ScoredCandidate] = []
    for cand in candidates:
        scored = blend_candidate(cand, weights, features=features)
        if boost_rules and boost_ctx is not None:
            scored = apply_boosts(scored, boost_rules, boost_ctx(cand.book_id))
        out.append(scored)
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def diversify(
    scored: Sequence[ScoredCandidate],
    *,
    k: int,
    mmr_lambda: float = 0.75,
) -> list[ScoredCandidate]:
    """MMR re-rank for diversity over the candidates' content vectors (§8.4).

    Reuses ``app.memory.retrieval.mmr_rerank``: greedily pick the candidate that
    is relevant (its blended score) *and* dissimilar to what's already chosen.
    Candidates without a content vector can't be diversity-compared, so they are
    appended after the MMR pass in score order (still capped to ``k``).
    """
    if k <= 0 or not scored:
        return []
    with_vec = [s for s in scored if s.vector and any(x != 0.0 for x in s.vector)]
    without_vec = [s for s in scored if not (s.vector and any(x != 0.0 for x in s.vector))]

    selected: list[ScoredCandidate] = []
    if with_vec:
        # Normalize relevance to [0, 1] so MMR's relevance/similarity terms are
        # comparable (cosine is already in [-1, 1]); a flat set maps to 1.0.
        scores = [s.score for s in with_vec]
        lo, hi = min(scores), max(scores)
        span = hi - lo
        wrapped = [
            Scored(item=s, score=((s.score - lo) / span if span > 0 else 1.0), vector=s.vector)
            for s in with_vec
        ]
        ranked = mmr_rerank([], wrapped, k=k, lambda_=mmr_lambda)
        selected = [r.item for r in ranked]

    if len(selected) < k:
        selected.extend(without_vec[: k - len(selected)])
    return selected[:k]


def to_recommendations(
    scored: Sequence[ScoredCandidate],
    *,
    features: Mapping[str, BookFeatures] | None = None,
) -> list[Recommendation]:
    """Project re-ranked candidates into the final 1-based, explained list."""
    recs: list[Recommendation] = []
    for i, cand in enumerate(scored, start=1):
        feat = features.get(cand.book_id) if features else None
        recs.append(
            Recommendation(
                book_id=cand.book_id,
                rank=i,
                score=cand.score,
                title=feat.title if feat else "",
                author=feat.author if feat else None,
                reasons=cand.reasons,
                explanation=reason_synth.explain(cand.reasons, features=features),
            )
        )
    return recs


def default_boost_rules() -> list[BoostRule]:
    """The default business-rule boosts (same-author + shared-tag affinity)."""
    return [same_author_boost(), shared_tag_boost()]


__all__ = [
    "BoostContext",
    "BoostRule",
    "apply_boosts",
    "blend_candidate",
    "default_boost_rules",
    "diversify",
    "same_author_boost",
    "score_candidates",
    "shared_tag_boost",
    "to_recommendations",
]
