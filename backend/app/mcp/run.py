"""``python -m app.mcp.run`` — run the MCP canon-memory server with real services.

Wires the real dependencies (the DB session factory, the multimodal embeddings
provider, the object store, and the persistent budget) into :class:`MemoryTools`
and serves the §8.3 tool surface. The render/queue and Adapter seams default to
:class:`NotWired` implementations — Phase 8 injects the real render backend; this
is an explicit DI seam, while every other tool is fully functional.

Usage:
    python -m app.mcp.run            # stdio transport (default)
    python -m app.mcp.run --http     # streamable-HTTP transport
"""

from __future__ import annotations

import argparse

import anyio

from app.core.config import get_settings
from app.db.session import get_session
from app.mcp.server import build_streamable_http_app, run_stdio
from app.mcp.tools import MemoryTools
from app.memory.budget_service import BudgetLimits
from app.memory.interfaces import NotWiredRenderEnqueuer, NotWiredShotPlanner


def build_default_tools() -> MemoryTools:
    """Construct :class:`MemoryTools` wired to the real providers and stores."""
    # Imported lazily so importing this module does not construct provider
    # clients (and so tests can build MemoryTools with doubles instead).
    from app.providers import create_providers
    from app.storage.object_store import ObjectStore

    settings = get_settings()
    providers = create_providers(settings)
    return MemoryTools(
        embedder=providers.embeddings,
        session_factory=get_session,
        blob_store=ObjectStore.from_settings(settings),
        limits=BudgetLimits.from_settings(settings),
        enqueuer=NotWiredRenderEnqueuer(),
        planner=NotWiredShotPlanner(),
    )


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
