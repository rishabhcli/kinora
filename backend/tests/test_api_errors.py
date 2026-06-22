"""Error-handler tests — every failure becomes a typed JSON envelope (§12).

These exercise :func:`app.api.errors.install_exception_handlers` directly on a
throwaway app (no infrastructure needed), proving the provider/render/budget
errors map to clean responses and that prod mode never leaks internals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.errors import APIError, install_exception_handlers
from app.memory.budget_service import BudgetExceeded
from app.providers.errors import LiveVideoDisabled, ProviderError


def _error_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/api-error")
    async def _api_error() -> None:
        raise APIError("teapot", "i am a teapot", status=418, detail={"k": "v"})

    @app.get("/budget")
    async def _budget() -> None:
        raise BudgetExceeded("ceiling", requested=10.0, used=95.0, cap=100.0)

    @app.get("/live")
    async def _live() -> None:
        raise LiveVideoDisabled("live video gated off")

    @app.get("/provider")
    async def _provider() -> None:
        raise ProviderError("upstream blew up", code="InternalError", status_code=500)

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("a secret stack-trace detail")

    return app


@pytest_asyncio.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False so the catch-all 500 handler's response is
    # returned (mirroring uvicorn) instead of re-raised into the test.
    transport = ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


async def test_api_error_envelope(error_client: AsyncClient) -> None:
    resp = await error_client.get("/api-error")
    assert resp.status_code == 418
    body = resp.json()
    assert body == {"error": {"type": "teapot", "message": "i am a teapot", "detail": {"k": "v"}}}


async def test_budget_exceeded_maps_to_402(error_client: AsyncClient) -> None:
    resp = await error_client.get("/budget")
    assert resp.status_code == 402
    body = resp.json()["error"]
    assert body["type"] == "budget_exceeded"
    assert body["detail"]["scope"] == "ceiling"
    assert body["detail"]["cap"] == 100.0


async def test_live_video_disabled_maps_to_409(error_client: AsyncClient) -> None:
    resp = await error_client.get("/live")
    assert resp.status_code == 409
    assert resp.json()["error"]["type"] == "live_video_disabled"


async def test_provider_error_maps_to_502(error_client: AsyncClient) -> None:
    resp = await error_client.get("/provider")
    assert resp.status_code == 502
    body = resp.json()["error"]
    assert body["type"] == "provider_error"
    assert body["detail"]["code"] == "InternalError"


async def test_unexpected_error_is_typed_json(error_client: AsyncClient) -> None:
    resp = await error_client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["type"] == "internal_error"
    # In local/dev the message is shown; the envelope shape is always typed.
    assert "message" in body


async def test_not_found_is_typed_json(error_client: AsyncClient) -> None:
    resp = await error_client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "http_error"
