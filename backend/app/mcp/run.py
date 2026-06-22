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

    tools = build_default_tools()
    if args.http:
        import uvicorn

        uvicorn.run(build_streamable_http_app(tools), host=args.host, port=args.port)
    else:
        anyio.run(run_stdio, tools)


if __name__ == "__main__":
    main()
