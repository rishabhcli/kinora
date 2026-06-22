"""The deployed MCP server (``python -m app.mcp.run``): real wiring + HTTP auth.

* Fix 2 — the tool layer built by ``app.mcp.run.build_default_tools`` must use the
  **real** RedisRenderEnqueuer + Adapter (never the ``NotWired`` DI placeholders),
  so the compose ``mcp`` service exposes fully-functional ``shot.render`` /
  ``shot.plan`` instead of raising at runtime.
* Fix 3 — the streamable-HTTP MCP is a control surface, so it is gated by
  ``MCP_AUTH_TOKEN`` (401 without a matching bearer, through to the MCP layer with
  one) and refuses to start unauthenticated outside ``local``.

These run with no infrastructure: the composition root opens no sockets at build
time, and the bearer checks are exercised at the ASGI layer.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.agents.adapter import Adapter
from app.core.config import Settings
from app.mcp.run import build_default_tools
from app.mcp.server import BearerAuthMiddleware, build_streamable_http_app
from app.memory.interfaces import NotWiredRenderEnqueuer, NotWiredShotPlanner
from app.queue.enqueuer import RedisRenderEnqueuer

# --------------------------------------------------------------------------- #
# Fix 2: the deployed tool layer is fully real (no NotWired)
# --------------------------------------------------------------------------- #


def test_deployed_tools_use_real_enqueuer_and_planner() -> None:
    tools = build_default_tools()
    # shot.render -> the real Redis priority-queue enqueuer (not the placeholder).
    assert isinstance(tools._enqueuer, RedisRenderEnqueuer)
    assert not isinstance(tools._enqueuer, NotWiredRenderEnqueuer)
    # shot.plan -> the real Adapter (not the placeholder that raises NotWired).
    assert isinstance(tools._planner, Adapter)
    assert not isinstance(tools._planner, NotWiredShotPlanner)


# --------------------------------------------------------------------------- #
# Fix 3: bearer auth on the streamable-HTTP MCP
# --------------------------------------------------------------------------- #


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    body = b'{"ok":true}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def test_bearer_middleware_rejects_without_and_admits_with_token() -> None:
    app = BearerAuthMiddleware(_ok_app, token="topsecret")
    transport = ASGITransport(app=cast("Any", app))
    async with AsyncClient(transport=transport, base_url="http://mcp") as http:
        # No bearer -> 401.
        no_token = await http.get("/mcp")
        assert no_token.status_code == 401
        # Wrong bearer -> 401.
        wrong = await http.get("/mcp", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        # Correct bearer -> reaches the inner app (200).
        ok = await http.get("/mcp", headers={"Authorization": "Bearer topsecret"})
        assert ok.status_code == 200
        assert ok.json() == {"ok": True}


def test_streamable_http_refuses_to_start_unauthenticated_outside_local() -> None:
    settings = Settings(
        dashscope_api_key="test",
        app_env="production",
        jwt_secret="a-real-production-secret-32-bytes-x",
        mcp_auth_token=None,
    )
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        build_streamable_http_app(build_default_tools(), settings=settings)


async def test_streamable_http_app_rejects_unauthenticated_request() -> None:
    settings = Settings(
        dashscope_api_key="test", app_env="local", mcp_auth_token="topsecret"
    )
    app = build_streamable_http_app(build_default_tools(), settings=settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://mcp") as http:
            # The bearer middleware rejects before the MCP layer is ever reached.
            resp = await http.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
            )
            assert resp.status_code == 401
