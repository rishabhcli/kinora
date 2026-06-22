"""Qwen / OpenAI function-call ("custom skill") definitions + dispatcher (§8.3, §14).

The design wants ``canon.query`` and ``shot.render`` wired as **custom Qwen
skills** so the agents (next phase) can hand them to Qwen function-calling and
have the model decide when to call memory. This module renders the tool surface
as OpenAI-compatible ``{"type": "function", ...}`` definitions (function names
use ``_`` since the function-call grammar forbids ``.``) and provides a
dispatcher that turns a model's ``(name, arguments)`` tool call back into a real
:meth:`MemoryTools.dispatch` invocation.
"""

from __future__ import annotations

import json
from typing import Any

from app.mcp.tools import TOOL_DEFS, TOOLS_BY_NAME, MemoryTools

#: The two tools the design names as custom skills (kinora.md §8.3 / §14).
FEATURED_SKILLS: tuple[str, ...] = ("canon.query", "shot.render")


def function_name(tool_name: str) -> str:
    """Map a dotted tool name to a function-call-safe name (``canon.query`` -> ``canon_query``)."""
    return tool_name.replace(".", "_")


def qwen_tool_definitions(*, featured_only: bool = False) -> list[dict[str, Any]]:
    """Return OpenAI/Qwen function-call definitions for the tool surface.

    Args:
        featured_only: When True, emit only the design's featured skills
            (``canon.query`` and ``shot.render``).
    """
    definitions: list[dict[str, Any]] = []
    for defn in TOOL_DEFS:
        if featured_only and defn.name not in FEATURED_SKILLS:
            continue
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": function_name(defn.name),
                    "description": defn.description,
                    "parameters": defn.input_model.model_json_schema(),
                },
            }
        )
    return definitions


class QwenSkillDispatcher:
    """Route a Qwen function call ``(name, arguments)`` to the tool implementation."""

    def __init__(self, tools: MemoryTools) -> None:
        self._tools = tools
        # Accept both the function-call name (canon_query) and the dotted tool
        # name (canon.query).
        self._by_function: dict[str, str] = {
            function_name(defn.name): defn.name for defn in TOOL_DEFS
        }

    def definitions(self, *, featured_only: bool = False) -> list[dict[str, Any]]:
        """The function-call definitions to pass to Qwen (see :func:`qwen_tool_definitions`)."""
        return qwen_tool_definitions(featured_only=featured_only)

    def resolve(self, name: str) -> str:
        """Resolve a function-call name (or dotted name) to the canonical tool name."""
        if name in TOOLS_BY_NAME:
            return name
        resolved = self._by_function.get(name)
        if resolved is None:
            raise ValueError(f"unknown skill: {name}")
        return resolved

    async def dispatch(self, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
        """Execute a tool call and return its JSON-serializable result.

        ``arguments`` may be a dict or a JSON string (as Qwen emits in
        ``tool_call.function.arguments``).
        """
        tool_name = self.resolve(name)
        if isinstance(arguments, str):
            parsed = json.loads(arguments) if arguments.strip() else {}
        else:
            parsed = dict(arguments)
        result = await self._tools.dispatch(tool_name, parsed)
        return result.model_dump(mode="json")


__all__ = [
    "FEATURED_SKILLS",
    "QwenSkillDispatcher",
    "function_name",
    "qwen_tool_definitions",
]
