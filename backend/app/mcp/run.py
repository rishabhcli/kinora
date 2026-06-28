"""``python -m app.mcp.run`` — run the MCP canon-memory server with REAL services.

Wires the deployed MCP through the **composition root** so the §8.3 tool surface
is fully real: ``shot.render`` enqueues through the real
:class:`~app.queue.enqueuer.RedisRenderEnqueuer` (the Redis priority queue) and
``shot.plan`` runs the real :class:`~app.agents.adapter.Adapter` — no
``NotWired`` seams. The streamable-HTTP transport is gated by ``MCP_AUTH_TOKEN``
(:func:`app.mcp.server.build_streamable_http_app`).

Usage:
    python -m app.mcp.run            # stdio transport (default)
    python -m app.mcp.run --http     # streamable-HTTP transport (:8765)
"""

from __future__ import annotations

import argparse
from typing import Any

import anyio

from app.composition import build_container
from app.mcp.server import build_streamable_http_app, run_stdio
from app.mcp.tools import MemoryTools


def build_default_tools() -> MemoryTools:
    """Construct :class:`MemoryTools` wired to the REAL render + planner seams.

    Reuses :func:`app.composition.build_container` (the single DI-seam
    satisfaction point) so the deployed server exposes the same fully-real tools
    the API gateway uses — never the ``NotWired`` placeholders.
    """
    return build_container().build_tools()


def build_http_app() -> Any:
    """The streamable-HTTP MCP app with §12 authorization + protocol layer wired on.

    The authorizer needs the same container the tools came from, so this builds
    both from one container (cheap to reuse — DI seams are lazy). When a
    per-client token table (``MCP_CLIENT_SCOPES``) is configured, the full
    per-client scoping is wired: a :class:`ContextScopedAuthorizer` reads the
    request identity that :class:`IdentityMiddleware` resolves from the bearer
    (so a read-only token cannot call a write/render tool). Otherwise the
    deployment keeps the book-existence authorizer + shared bearer.
    """
    from app.mcp.identity import ContextScopedAuthorizer

    container = build_container()
    tools = container.build_tools()
    settings = container.settings
    if settings.mcp_client_scopes:
        return build_streamable_http_app(
            tools,
            authorizer=ContextScopedAuthorizer(container.build_scoped_authorizer()),
            identity_resolver=container.build_mcp_identity_resolver(),
        )
    return build_streamable_http_app(tools, authorizer=container.build_mcp_authorizer())


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="app.mcp.run", description="Kinora MCP canon-memory server"
    )
    parser.add_argument(
        "--http", action="store_true", help="serve over streamable-HTTP instead of stdio"
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (--http only)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port (--http only)")
    args = parser.parse_args(argv)

    if args.http:
        import uvicorn

        uvicorn.run(build_http_app(), host=args.host, port=args.port)
    else:
        anyio.run(run_stdio, build_default_tools())


if __name__ == "__main__":
    main()
