"""Integration tests for the DB-backed recsys store + service + API.

These require the throwaway infra (run against the isolated ``kinora_recs_test``
DB on :5433, never the live ``kinora`` DB). They skip cleanly when
``KINORA_TEST_DATABASE_URL`` / ``_REDIS_URL`` / ``_S3_ENDPOINT_URL`` are unset,
so the unit suite still runs anywhere.

The deterministic :class:`~tests.conftest.FakeEmbedder` is injected on the
container, so no network/credits are spent: book feature embeddings are one-hot
vectors, and the taste fold / content recall operate on those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.composition import Container
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.recommendations.store import (
    BookFeatureRepo,
    InteractionRepo,
    RecommendationService,
    TasteVectorRepo,
)
from app.recommendations.types import InteractionKind, RecsConfig
from tests.conftest import register_login, requires_infra, user_id_for

pytestmark = requires_infra


def _onehot(axis: int, dim: int = 1152) -> list[float]:
    v = [0.0] * dim
    v[axis % dim] = 1.0
    return v


async def _seed_user(container: Container, user_id: str) -> None:
    """Insert a bare user row so the interaction FK (user_id → users.id) holds."""
    async with container.session_factory() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.invalid", hashed_password="x"))


async def _seed_book(
    container: Container, book_id: str, title: str, *, author: str | None = None
) -> None:
    async with container.session_factory() as session:
        await BookRepo(session).create(title=title, book_id=book_id, author=author)


@pytest.mark.asyncio
async def test_interaction_repo_logs_and_reads_back(container: Container) -> None:
    await _seed_user(container, "u1")
    await _seed_book(container, "b1", "One")
    async with container.session_factory() as session:
        repo = InteractionRepo(session)
        await repo.log(user_id="u1", book_id="b1", kind=InteractionKind.LIKE)
    async with container.session_factory() as session:
        events = await InteractionRepo(session).for_user("u1")
    assert len(events) == 1
    assert events[0].book_id == "b1"
    assert events[0].kind is InteractionKind.LIKE


@pytest.mark.asyncio
async def test_feature_repo_upsert_is_idempotent(container: Container) -> None:
    await _seed_book(container, "b1", "One")
    async with container.session_factory() as session:
        repo = BookFeatureRepo(session)
        await repo.upsert(book_id="b1", embedding=_onehot(3), popularity=2.0, tags=["fantasy"])
        await repo.upsert(book_id="b1", popularity=9.0)  # update only popularity
    async with container.session_factory() as session:
        corpus = await BookFeatureRepo(session).corpus()
    assert "b1" in corpus
    assert corpus["b1"].popularity == 9.0
    assert corpus["b1"].tags == ("fantasy",)  # preserved across the partial update
    assert corpus["b1"].has_embedding


@pytest.mark.asyncio
async def test_service_recommends_content_similar_book(container: Container) -> None:
    # Two fantasy books (same one-hot axis) + one unrelated; user finishes one.
    await _seed_user(container, "u1")
    await _seed_book(container, "fant1", "Fantasy One")
    await _seed_book(container, "fant2", "Fantasy Two")
    await _seed_book(container, "scifi", "Sci-Fi")
    async with container.session_factory() as session:
        feats = BookFeatureRepo(session)
        await feats.upsert(book_id="fant1", embedding=_onehot(1), tags=["fantasy"])
        await feats.upsert(book_id="fant2", embedding=_onehot(1), tags=["fantasy"])
        await feats.upsert(book_id="scifi", embedding=_onehot(2), tags=["scifi"])
        await InteractionRepo(session).log(
            user_id="u1", book_id="fant1", kind=InteractionKind.FINISH
        )
    async with container.session_factory() as session:
        service = RecommendationService(session, config=RecsConfig(top_k=5))
        recs = await service.recommend("u1")
    ids = [r.book_id for r in recs]
    assert ids  # not empty
    assert "fant1" not in ids  # the engaged book is never recommended back
    # The same-axis fantasy sibling outranks the orthogonal sci-fi book.
    assert ids.index("fant2") < ids.index("scifi")


@pytest.mark.asyncio
async def test_service_log_interaction_folds_taste_vector(container: Container) -> None:
    await _seed_user(container, "u1")
    await _seed_book(container, "b1", "One")
    async with container.session_factory() as session:
        await BookFeatureRepo(session).upsert(book_id="b1", embedding=_onehot(5))
    async with container.session_factory() as session:
        service = RecommendationService(session)
        await service.log_interaction(user_id="u1", book_id="b1", kind=InteractionKind.LIKE)
    async with container.session_factory() as session:
        acc = await TasteVectorRepo(session).get("u1")
    assert acc is not None
    assert acc.event_count == 1
    assert not acc.is_cold  # a positive like established taste mass


@pytest.mark.asyncio
async def test_service_cold_user_gets_popularity_floor(container: Container) -> None:
    # A popular book nobody-this-user engaged with should still surface.
    await _seed_book(container, "pop", "Popular")
    async with container.session_factory() as session:
        await BookFeatureRepo(session).upsert(book_id="pop", embedding=_onehot(7), popularity=50.0)
    async with container.session_factory() as session:
        service = RecommendationService(session, config=RecsConfig(top_k=5))
        recs = await service.recommend("brand_new_user")
    assert [r.book_id for r in recs] == ["pop"]
    assert recs[0].reasons[0].kind.value == "popular"


@pytest.mark.asyncio
async def test_service_neighbourhood_drives_collaborative(container: Container) -> None:
    # u1 & u2 both finish fant1; u2 also likes fant2. u1 should get fant2 via CF.
    await _seed_user(container, "u1")
    await _seed_user(container, "u2")
    for bid, title in [("fant1", "F1"), ("fant2", "F2")]:
        await _seed_book(container, bid, title)
    async with container.session_factory() as session:
        feats = BookFeatureRepo(session)
        await feats.upsert(book_id="fant1", embedding=_onehot(1))
        await feats.upsert(book_id="fant2", embedding=_onehot(1))
        repo = InteractionRepo(session)
        now = datetime.now(UTC)
        await repo.log(user_id="u1", book_id="fant1", kind=InteractionKind.FINISH, at=now)
        await repo.log(user_id="u2", book_id="fant1", kind=InteractionKind.FINISH, at=now)
        await repo.log(
            user_id="u2", book_id="fant2", kind=InteractionKind.LIKE, at=now - timedelta(days=1)
        )
    async with container.session_factory() as session:
        service = RecommendationService(session, config=RecsConfig(top_k=5))
        recs = await service.recommend("u1")
    fant2 = next((r for r in recs if r.book_id == "fant2"), None)
    assert fant2 is not None
    kinds = {reason.kind.value for reason in fant2.reasons}
    assert "collaborative" in kinds


# --------------------------------------------------------------------------- #
# API route tests (owner-scoped, over the gateway)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_api_recommendations_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/recommendations")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_log_interaction_then_recommend(
    api_client: AsyncClient, container: Container
) -> None:
    headers = await register_login(api_client, "reader@example.com")
    uid = await user_id_for(api_client, headers)

    # Seed a small fantasy corpus owned by the reader.
    for bid, title, axis in [("fant1", "F1", 1), ("fant2", "F2", 1), ("scifi", "S", 2)]:
        async with container.session_factory() as session:
            await BookRepo(session).create(title=title, book_id=bid, user_id=uid)
            await BookFeatureRepo(session).upsert(book_id=bid, embedding=_onehot(axis))

    # Log a FINISH on fant1 via the API.
    resp = await api_client.post(
        "/api/recommendations/interactions",
        json={"book_id": "fant1", "kind": "finish"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text

    # Recommendations should now surface the fantasy sibling, explained.
    resp = await api_client.get("/api/recommendations?limit=5", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == uid
    ids = [r["book_id"] for r in body["recommendations"]]
    assert "fant1" not in ids
    assert "fant2" in ids
    fant2 = next(r for r in body["recommendations"] if r["book_id"] == "fant2")
    assert fant2["explanation"]  # a non-empty natural-language reason
    assert fant2["reasons"]


@pytest.mark.asyncio
async def test_api_why_endpoint(api_client: AsyncClient, container: Container) -> None:
    headers = await register_login(api_client, "why@example.com")
    uid = await user_id_for(api_client, headers)
    for bid, axis in [("a", 1), ("b", 1)]:
        async with container.session_factory() as session:
            await BookRepo(session).create(title=bid.upper(), book_id=bid, user_id=uid)
            await BookFeatureRepo(session).upsert(book_id=bid, embedding=_onehot(axis))
    await api_client.post(
        "/api/recommendations/interactions",
        json={"book_id": "a", "kind": "like"},
        headers=headers,
    )
    resp = await api_client.get("/api/recommendations/why/b", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["book_id"] == "b"
    assert body["recommended"] is True
    assert body["reasons"]

    # An unknown book reports honestly rather than fabricating reasons.
    resp = await api_client.get("/api/recommendations/why/ghost", headers=headers)
    assert resp.json()["recommended"] is False
