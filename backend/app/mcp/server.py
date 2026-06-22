"""The MCP server — the §8.3 tool surface over the official Model Context Protocol.

``build_server`` registers every tool from :data:`app.mcp.tools.TOOL_DEFS` with
its JSON Schema (derived from the pydantic input model) and routes calls through
:meth:`MemoryTools.dispatch`. The server is transport-agnostic: ``run_stdio``
serves it over stdio (the canonical MCP transport, used by ``python -m
app.mcp.run``), and ``build_streamable_http_app`` wraps it as a Starlette ASGI
app for streamable-HTTP deployments.

**Security (kinora.md §12).** The streamable-HTTP MCP is a control surface (it can
enqueue real renders that spend metered video-seconds), so it is *not* exposed
unauthenticated:

* when ``MCP_AUTH_TOKEN`` is set, :class:`BearerAuthMiddleware` requires a
  matching ``Authorization: Bearer <token>`` on every request (401 otherwise);
* outside ``local`` the token is **mandatory** — :func:`build_streamable_http_app`
  refuses to start the HTTP MCP without it (network isolation alone is not a
  control-plane auth story).

Per-tool/per-tenant authorization (e.g. asserting the caller owns ``book_id``)
is a structured seam — :class:`ToolAuthorizer`, threaded into every dispatch via
``build_server(..., authorizer=...)``. The bearer gate + network isolation close
the hole today; threading a real caller identity from the bearer subject into a
per-book ownership check is the remaining follow-up.
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Any, Protocol

import mcp.types as types
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.mcp.tools import TOOL_DEFS, MemoryTools
from mcp.server import Server

logger = get_logger("app.mcp.server")

DEFAULT_SERVER_NAME = "kinora-canon-memory"


class ToolAuthorizer(Protocol):
    """Per-call authorization seam for the MCP tool surface (kinora.md §12).

    Invoked before every tool dispatch with the tool name and its (validated)
    arguments — which include ``book_id`` for the book-scoped tools — so a real
    implementation can resolve the caller's identity and assert per-book
    ownership. Raise to deny (the error surfaces to the client); return to allow.
    """

    async def authorize(self, tool_name: str, arguments: dict[str, Any]) -> None: ...


def build_server(
    tools: MemoryTools,
    *,
    name: str = DEFAULT_SERVER_NAME,
    authorizer: ToolAuthorizer | None = None,
) -> Server[Any, Any]:
    """Build an MCP :class:`Server` exposing the memory tools.

    ``authorizer`` (optional) is consulted before each dispatch — the per-book
    ownership seam (§12). When ``None`` the surface is open (the bearer gate +
    network isolation are the active controls for the HTTP transport).
    """
    server: Server[Any, Any] = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=defn.name,
                description=defn.description,
                inputSchema=defn.input_model.model_json_schema(),
            )
            for defn in TOOL_DEFS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if authorizer is not None:
            await authorizer.authorize(name, arguments)
        result = await tools.dispatch(name, arguments)
        # Returning a dict gives the client structuredContent plus a JSON text block.
        return result.model_dump(mode="json")

    return server


async def run_stdio(tools: MemoryTools, *, name: str = DEFAULT_SERVER_NAME) -> None:
    """Serve the memory tools over stdio (the canonical MCP transport)."""
    server = build_server(tools, name=name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


class BearerAuthMiddleware:
    """ASGI middleware enforcing ``Authorization: Bearer <token>`` (constant-time).

    Non-HTTP scopes (``lifespan``, ``websocket``) pass through untouched; HTTP
    requests without an exact bearer match get a 401 before reaching the MCP
    manager. The comparison uses :func:`hmac.compare_digest` so it does not leak
    the token length/prefix via timing.
    """

    def __init__(self, app: Any, *, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode("latin-1")
        if not hmac.compare_digest(provided, self._expected):
            await self._reject(send)
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"error":"unauthorized","detail":"missing or invalid bearer token"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer realm="kinora-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_streamable_http_app(
    tools: MemoryTools,
    *,
    name: str = DEFAULT_SERVER_NAME,
    json_response: bool = True,
    settings: Settings | None = None,
    authorizer: ToolAuthorizer | None = None,
) -> Any:
    """Wrap the server as a Starlette ASGI app for streamable-HTTP transport.

    Mounts the MCP endpoint at ``/mcp`` and gates it with
    :class:`BearerAuthMiddleware` when ``MCP_AUTH_TOKEN`` is configured. Outside
    ``local`` the token is mandatory: this refuses to start an unauthenticated
    control surface in any non-local environment (§12). Imported lazily so stdio
    deployments do not require Starlette at import time.

    Raises:
        RuntimeError: when ``app_env`` is not ``local`` and ``MCP_AUTH_TOKEN`` is unset.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    settings = settings or get_settings()
    auth_token = settings.mcp_auth_token
    if not auth_token and not settings.is_local:
        raise RuntimeError(
            "refusing to start the streamable-HTTP MCP without MCP_AUTH_TOKEN "
            f"(app_env={settings.app_env!r}): an unauthenticated control surface "
            "must not run outside 'local'"
        )

    server = build_server(tools, name=name, authorizer=authorizer)
    manager = StreamableHTTPSessionManager(app=server, json_response=json_response, stateless=True)

    async def handle(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    if auth_token:
        app.add_middleware(BearerAuthMiddleware, token=auth_token)
        logger.info("mcp.http.auth_enabled")
    else:
        # Only reachable in local (the non-local case raised above).
        logger.warning("mcp.http.auth_disabled", env=settings.app_env)
    return app


__all__ = [
    "DEFAULT_SERVER_NAME",
    "BearerAuthMiddleware",
    "ToolAuthorizer",
    "build_server",
    "build_streamable_http_app",
    "run_stdio",
]
