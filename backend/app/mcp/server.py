"""The MCP server — the §8.3 tool surface over the official Model Context Protocol.

``build_server`` registers every tool from :data:`app.mcp.tools.TOOL_DEFS` with
its JSON Schema (derived from the pydantic input model) and routes calls through
:meth:`MemoryTools.dispatch`. The server is transport-agnostic: ``run_stdio``
serves it over stdio (the canonical MCP transport, used by ``python -m
app.mcp.run``), and ``build_streamable_http_app`` wraps it as a Starlette ASGI
app for streamable-HTTP deployments.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as types
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from app.mcp.tools import TOOL_DEFS, MemoryTools
from mcp.server import Server

DEFAULT_SERVER_NAME = "kinora-canon-memory"


def build_server(tools: MemoryTools, *, name: str = DEFAULT_SERVER_NAME) -> Server[Any, Any]:
    """Build an MCP :class:`Server` exposing the memory tools."""
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
        result = await tools.dispatch(name, arguments)
        # Returning a dict gives the client structuredContent plus a JSON text block.
        return result.model_dump(mode="json")

    return server


async def run_stdio(tools: MemoryTools, *, name: str = DEFAULT_SERVER_NAME) -> None:
    """Serve the memory tools over stdio (the canonical MCP transport)."""
    server = build_server(tools, name=name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def build_streamable_http_app(
    tools: MemoryTools, *, name: str = DEFAULT_SERVER_NAME, json_response: bool = True
) -> Any:
    """Wrap the server as a Starlette ASGI app for streamable-HTTP transport.

    Mounts the MCP endpoint at ``/mcp``. Imported lazily so stdio deployments do
    not require Starlette at import time.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = build_server(tools, name=name)
    manager = StreamableHTTPSessionManager(app=server, json_response=json_response, stateless=True)

    async def handle(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


__all__ = [
    "DEFAULT_SERVER_NAME",
    "build_server",
    "build_streamable_http_app",
    "run_stdio",
]
