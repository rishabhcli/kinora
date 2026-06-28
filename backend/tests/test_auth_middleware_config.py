"""Unit tests for the CSRF middleware + the auth config guards (no infra)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.auth.middleware import CsrfMiddleware
from app.core.config import DEFAULT_API_KEY_PEPPER, Settings


def _csrf_app() -> Starlette:
    async def ok(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/write", ok, methods=["POST"]), Route("/read", ok, methods=["GET"])]
    )
    app.add_middleware(CsrfMiddleware, enabled=True)
    return app


async def _client(app: Starlette) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_csrf_exempts_bearer_requests() -> None:
    async with await _client(_csrf_app()) as c:
        # A Bearer request with a session cookie is exempt (not CSRF-exposed).
        r = await c.post(
            "/write",
            headers={"Authorization": "Bearer tok", "Cookie": "session=abc"},
        )
        assert r.status_code == 200


async def test_csrf_blocks_cookie_write_without_token() -> None:
    async with await _client(_csrf_app()) as c:
        r = await c.post("/write", headers={"Cookie": "session=abc"})
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "csrf_failed"


async def test_csrf_allows_cookie_write_with_matching_token() -> None:
    async with await _client(_csrf_app()) as c:
        token = "csrf-token-value"
        r = await c.post(
            "/write",
            headers={"Cookie": f"session=abc; kinora_csrf={token}", "X-CSRF-Token": token},
        )
        assert r.status_code == 200


async def test_csrf_rejects_mismatched_token() -> None:
    async with await _client(_csrf_app()) as c:
        r = await c.post(
            "/write",
            headers={"Cookie": "session=abc; kinora_csrf=real", "X-CSRF-Token": "fake"},
        )
        assert r.status_code == 403


async def test_csrf_no_cookie_auth_is_unguarded() -> None:
    async with await _client(_csrf_app()) as c:
        # No session cookie at all → not cookie-auth → not CSRF-checked.
        r = await c.post("/write")
        assert r.status_code == 200


async def test_csrf_safe_methods_pass_and_set_cookie() -> None:
    async with await _client(_csrf_app()) as c:
        r = await c.get("/read")
        assert r.status_code == 200
        # A fresh CSRF cookie is issued for the SPA to read + echo.
        assert "kinora_csrf" in r.headers.get("set-cookie", "")


async def test_csrf_disabled_passthrough() -> None:
    app = Starlette(routes=[Route("/write", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    app.add_middleware(CsrfMiddleware, enabled=False)
    async with await _client(app) as c:
        r = await c.post("/write", headers={"Cookie": "session=abc"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Config guards
# --------------------------------------------------------------------------- #


def test_local_keeps_default_pepper() -> None:
    s = Settings(dashscope_api_key="test", app_env="local")
    assert s.api_key_pepper == DEFAULT_API_KEY_PEPPER


def test_nonlocal_derives_pepper_from_jwt_secret() -> None:
    real = "a-real-and-sufficiently-long-jwt-secret-value"
    s = Settings(dashscope_api_key="test", app_env="production", jwt_secret=real)
    assert s.api_key_pepper != DEFAULT_API_KEY_PEPPER
    assert s.api_key_pepper.startswith("derived:")
    # Deterministic: same secret → same derived pepper.
    s2 = Settings(dashscope_api_key="test", app_env="production", jwt_secret=real)
    assert s.api_key_pepper == s2.api_key_pepper


def test_nonlocal_explicit_pepper_is_kept() -> None:
    s = Settings(
        dashscope_api_key="test",
        app_env="production",
        jwt_secret="another-real-secret-value-here-long-enough",
        api_key_pepper="my-explicit-pepper",
    )
    assert s.api_key_pepper == "my-explicit-pepper"


def test_nonlocal_default_jwt_still_refuses() -> None:
    with pytest.raises(ValueError):
        Settings(dashscope_api_key="test", app_env="production")
