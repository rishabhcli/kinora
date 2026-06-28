"""FinOps API route tests (kinora.md §11.1).

The ``/simulate`` endpoint only needs the active tier policy
(``container.finops_policy``) and the pure harness, so it is tested with a fake
container + bypassed auth (no Postgres/Redis), mirroring ``test_api_optim.py``.
The DB-backed ``/budget`` / ``/cost`` / ``/reconcile`` / ``/forecast`` endpoints
(they read current usage from the ledger) are covered through the gateway in the
infra-gated suite.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from app.api.deps import get_container, get_current_user
from app.api.routes.finops import router
from app.composition import Container
from app.core.config import Settings
from app.finops.tiers import BudgetTierPolicy
from tests.conftest import requires_infra, seed_owned_book, user_id_for


class _FakeContainer:
    """Just enough container surface for the pure finops endpoints."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.finops_policy = BudgetTierPolicy.from_settings(settings)


def _app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(
        dashscope_api_key="test",
        budget_ceiling_video_s=1650.0,
        budget_per_session_s=300.0,
        budget_per_scene_s=90.0,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    fake = _FakeContainer(settings)
    app.dependency_overrides[get_container] = lambda: fake
    app.dependency_overrides[get_current_user] = lambda: object()
    return app


def test_simulate_default_suite_stays_in_budget() -> None:
    resp = TestClient(_app()).post("/api/finops/simulate", json={"max_ticks": 2000})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["any_cap_breached"] is False
    assert len(data["results"]) >= 1


def test_simulate_custom_reader() -> None:
    body = {
        "readers": [
            {"label": "binge", "velocity_wps": 8.0, "total_words": 200000, "promotion_rate": 1.0}
        ],
        "max_ticks": 3000,
    }
    resp = TestClient(_app()).post("/api/finops/simulate", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["any_cap_breached"] is False
    assert data["results"][0]["label"] == "binge"


def test_simulate_tiny_cap_never_breaches() -> None:
    tiny = Settings(
        dashscope_api_key="test",
        budget_ceiling_video_s=20.0,
        budget_per_session_s=20.0,
        budget_per_scene_s=20.0,
    )
    body = {
        "readers": [
            {"label": "steady", "velocity_wps": 4.0, "total_words": 40000}
        ],
        "max_ticks": 2000,
    }
    resp = TestClient(_app(tiny)).post("/api/finops/simulate", json=body)
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["cap_breached"] is False
    assert result["video_seconds_spent"] <= 20.0 + 1e-6


# --------------------------------------------------------------------------- #
# DB-backed gateway tests (require throwaway Postgres + Redis + MinIO)
# --------------------------------------------------------------------------- #


@requires_infra
async def test_budget_endpoint_reflects_reservations(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Budget Book")

    # Reserve some video-seconds against this tenant's book via the FinOps service.
    # (No session_id: budget_ledger.session_id is an FK, and the endpoint scopes
    # to the tenant + global caps without a session.)
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        await finops.reserve(40.0, tenant_id=uid, book_id=book_id)

    resp = await api_client.get("/api/finops/budget", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == uid
    scopes = {s["scope"]: s for s in body["statuses"]}
    assert scopes["global"]["used_s"] >= 40.0
    assert scopes["tenant"]["used_s"] >= 40.0


@requires_infra
async def test_cost_endpoint_reports_attributed_usd(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    from decimal import Decimal

    from app.providers.types import Usage

    uid = await user_id_for(api_client, auth_headers)
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Cost Book")

    async with container.session_factory() as db:
        finops = container.build_finops(db)
        await finops.record_usage_cost(
            Usage(model="wan2.7-i2v", operation="video", video_seconds=5.0),
            Decimal("0.60"),
            tenant_id=uid,
            book_id=book_id,
            shot_id="shot_1",
        )

    resp = await api_client.get("/api/finops/cost", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["cost_usd"]) >= Decimal("0.60")
    assert "generator" in body["by_agent_usd"]


@requires_infra
async def test_reconcile_endpoint_flags_drift(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Recon Book")
    async with container.session_factory() as db:
        finops = container.build_finops(db)
        res = await finops.reserve(5.0, book_id=book_id)
        await finops.commit(res, actual_seconds=5.0)
        # No cost row -> drift.

    resp = await api_client.get(
        "/api/finops/reconcile", headers=auth_headers, params={"book_id": book_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled"] is False
    assert body["drift_s"] == pytest.approx(-5.0)


@requires_infra
async def test_forecast_endpoint_governance_preview(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    await seed_owned_book(api_client, container, auth_headers, title="Forecast Book")
    body = {
        "trajectory": {"velocity_wps": 4.0, "words_remaining": 4000},
        "upcoming": [
            {"shot_id": "s0", "video_seconds": 5.0},
            {"shot_id": "s1", "video_seconds": 5.0},
        ],
    }
    resp = await api_client.post("/api/finops/forecast", headers=auth_headers, json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["recommendation"] in {"promote", "optimize", "halt"}
    assert "burn" in data["forecast"]
    assert "assignments" in data["plan"]


@requires_infra
async def test_finops_endpoints_require_auth(api_client: AsyncClient) -> None:
    for path in ("/api/finops/budget", "/api/finops/cost", "/api/finops/reconcile"):
        resp = await api_client.get(path)
        assert resp.status_code == 401
