"""Server-side recommendations engine (``backend/app/recommendations/``).

The recsys that ranks which *books* a Kinora reader should watch next, distinct
from any client-side discovery UI. It blends three signals — content similarity
over the §8 shared embeddings, collaborative filtering, and a recency-decayed
per-user taste vector — through a candidate-generation → scoring → re-ranking
pipeline with MMR diversity, business-rule boosts, cold-start fallbacks, and
explainable "because you read X" reasons. See ``DESIGN.md`` for the roadmap.

The math core is pure and deterministic (tested with fake/one-hot embeddings —
zero credits). The async, DB-backed :class:`~app.recommendations.store.RecommendationService`
is a thin shell over :class:`~app.recommendations.engine.RecommendationEngine`.
"""

from __future__ import annotations

from .engine import RecommendationEngine, make_config_from_settings
from .types import (
    BlendWeights,
    BookFeatures,
    Interaction,
    InteractionKind,
    Reason,
    ReasonKind,
    Recommendation,
    RecsConfig,
)

__all__ = [
    "BlendWeights",
    "BookFeatures",
    "Interaction",
    "InteractionKind",
    "Reason",
    "ReasonKind",
    "Recommendation",
    "RecommendationEngine",
    "RecsConfig",
    "make_config_from_settings",
]
