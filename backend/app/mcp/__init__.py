"""The MCP transport for the canon-memory service (kinora.md §8.3, §14).

Exposes the memory layer's §8.3 tool surface over the official Model Context
Protocol (:mod:`app.mcp.server`) and as Qwen custom-skill function-call
definitions (:mod:`app.mcp.skills`). :class:`~app.mcp.tools.MemoryTools` is the
single, SDK-agnostic implementation both transports dispatch through.
"""

from __future__ import annotations

from app.mcp.server import (
    DEFAULT_SERVER_NAME,
    build_server,
    build_streamable_http_app,
    run_stdio,
)
from app.mcp.skills import (
    FEATURED_SKILLS,
    QwenSkillDispatcher,
    function_name,
    qwen_tool_definitions,
)
from app.mcp.tools import TOOL_DEFS, TOOLS_BY_NAME, MemoryTools, ToolDef

__all__ = [
    "DEFAULT_SERVER_NAME",
    "FEATURED_SKILLS",
    "MemoryTools",
    "QwenSkillDispatcher",
    "TOOL_DEFS",
    "TOOLS_BY_NAME",
    "ToolDef",
    "build_server",
    "build_streamable_http_app",
    "function_name",
    "qwen_tool_definitions",
    "run_stdio",
]
