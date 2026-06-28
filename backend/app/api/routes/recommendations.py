"""Recommendations routes — "what should I watch next?" + interaction logging.

The HTTP surface over the server-side recsys (``app.recommendations``). Three
endpoints, all owner-scoped to the authenticated reader:

* ``GET  /recommendations`` — the ranked, explained "watch next" list for the
  caller (candidate-gen → score → re-rank over the recsys warehouse).
* ``POST /recommendations/interactions`` — log a reader↔book engagement signal
  (and fold it into the cached taste vector); the feedback that personalizes
  future recommendations.
* ``GET  /recommendations/why/{book_id}`` — the explainability detail: every
  signed reason behind a single recommended book, for a "why am I seeing this?"
  surface.

The pure ranking logic lives in the engine; these handlers only authenticate,
bind a unit-of-work session via ``container.build_recommendation_service``, and
project to the API contract.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.recommendations.types import InteractionKind

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


# --------------------------------------------------------------------------- #
# Response / request schemas
# --------------------------------------------------------------------------- #


class ReasonOut(BaseModel):
    """One explainable contribution to a recommendation's score."""

    kind: str
    contribution: float
    seed_book_id: str | None = None
    seed_title: str | None = None
    detail: str | None = None


class RecommendationOut(BaseModel):
    """A single ranked, explained recommendation."""

    book_id: str
    rank: int
    score: float
    title: str = ""
    author: str | None = None
    explanation: str = ""
    reasons: list[ReasonOut] = Field(default_factory=list)


class RecommendationsResponse(BaseModel):
    """The ranked recommendation list for the caller."""

    user_id: str
    count: int
    recommendations: list[RecommendationOut]


class LogInteractionRequest(BaseModel):
    """A reader↔book engagement signal to record."""

    book_id: str
    kind: InteractionKind
    weight: float | None = Field(default=None, description="explicit feedback weight override")
    dwell_s: float | None = Field(default=None, ge=0.0, description="engagement dwell seconds")


class WhyResponse(BaseModel):
    """The reasons behind a single recommended book (explainability detail)."""

    book_id: str
    recommended: bool
    explanation: str
    reasons: list[ReasonOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("", response_model=RecommendationsResponse)
async def get_recommendations(
    container: ContainerDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RecommendationsResponse:
    """Return the ranked "watch next" recommendations for the authenticated reader."""
    async with container.session_factory() as session:
        service = container.build_recommendation_service(session)
        recs = await service.recommend(user.id, top_k=limit)
    return RecommendationsResponse(
        user_id=user.id,
        count=len(recs),
        recommendations=[RecommendationOut(**r.to_dict()) for r in recs],
    )


@router.post(
    "/interactions",
    status_code=204,
    dependencies=[Depends(write_rate_limit)],
)
async def log_interaction(
    body: LogInteractionRequest, container: ContainerDep, user: CurrentUser
) -> None:
    """Record a reader↔book engagement signal (and refresh the taste vector)."""
    async with container.session_factory() as session:
        service = container.build_recommendation_service(session)
        await service.log_interaction(
            user_id=user.id,
            book_id=body.book_id,
            kind=body.kind,
            weight=body.weight,
            dwell_s=body.dwell_s,
        )


@router.get("/why/{book_id}", response_model=WhyResponse)
async def why_recommended(book_id: str, container: ContainerDep, user: CurrentUser) -> WhyResponse:
    """Explain why a book is (or isn't) recommended to the caller.

    Runs the ranker over a generous list and locates ``book_id`` so the response
    carries the exact signed reasons that put it on (or off) the list.
    """
    async with container.session_factory() as session:
        service = container.build_recommendation_service(session)
        recs = await service.recommend(user.id, top_k=100)
    match = next((r for r in recs if r.book_id == book_id), None)
    if match is None:
        # Not in the top-100; report honestly rather than fabricating reasons.
        return WhyResponse(
            book_id=book_id,
            recommended=False,
            explanation="Not currently among your top recommendations",
            reasons=[],
        )
    payload = match.to_dict()
    return WhyResponse(
        book_id=book_id,
        recommended=True,
        explanation=payload["explanation"],
        reasons=[ReasonOut(**r) for r in payload["reasons"]],
    )


__all__ = ["router"]
