"""Security hardening: the SecurityHeadersMiddleware stamps every response."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.api.middleware import SecurityHeadersMiddleware


async def test_security_headers_present_on_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # cdn.jsdelivr.net is allowed so the OpenAPI docs UI still loads.
    assert "https://cdn.jsdelivr.net" in csp


async def test_no_hsts_in_local_env(client: AsyncClient) -> None:
    # Tests run with APP_ENV=local → never pin HTTPS for localhost dev.
    response = await client.get("/health")
    assert "strict-transport-security" not in response.headers


async def test_security_headers_present_on_error_responses(client: AsyncClient) -> None:
    response = await client.get("/no-such-route-xyz")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"]


async def _probe(*, hsts: bool) -> dict[str, str]:
    async def ok(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/x", ok)])
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/x")
    return dict(response.headers)


async def test_hsts_emitted_only_when_enabled() -> None:
    with_hsts = await _probe(hsts=True)
    without_hsts = await _probe(hsts=False)
    assert with_hsts["strict-transport-security"].startswith("max-age=63072000")
    assert "strict-transport-security" not in without_hsts
    # The other headers are present regardless of the HSTS toggle.
    assert with_hsts["x-frame-options"] == "DENY"
    assert without_hsts["x-frame-options"] == "DENY"
