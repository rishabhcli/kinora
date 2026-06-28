"""A typed Python client SDK for the Kinora canon MCP server (§8.3).

Agents and tools talk to the canon over MCP. The raw protocol round-trip (build a
``CallToolRequest``, parse ``structuredContent`` back out) is tedious and untyped;
this SDK gives callers a typed surface — one method per tool, each taking the
tool's pydantic **input** model (or a dict) and returning its pydantic **output**
model — over a pluggable :class:`Transport`.

Two transports ship:

* :class:`InProcessTransport` — calls :meth:`MemoryTools.dispatch` directly
  through the same validation + (optional) authorization the server applies. The
  fast path for in-process callers (the agent crew) and the conformance suite;
  no socket, no serialization round-trip, but the *same* contract.
* :class:`SessionTransport` — wraps an already-connected ``mcp.ClientSession``
  (stdio or streamable-HTTP), so the SDK works against a remote canon server
  too. The protocol round-trip is hidden behind the same typed methods.

The SDK validates requests (so a bad call fails locally with a typed error) and,
on the in-process path, validates responses; the typed return model is the
contract either way. Nothing here renders or spends — it is a client.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.mcp.errors import MCPError, MethodNotFoundError, to_error_body
from app.mcp.registry import ToolCatalog, ToolMeta, ToolVersion, default_catalog
from app.mcp.server import VERSION_META_KEY
from app.mcp.validation import SchemaValidator

InModel = TypeVar("InModel", bound=BaseModel)


class Transport(Protocol):
    """How a :class:`KinoraMCPClient` actually reaches a tool."""

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke ``name`` and return its JSON-serializable structured result.

        Implementations raise an :class:`~app.mcp.errors.MCPError` (or the
        transport's native error) on failure.
        """
        ...


class InProcessTransport:
    """Call ``MemoryTools.dispatch`` directly, in-process.

    Applies the same request validation + (optional) authorization the server
    applies, so an in-process caller cannot bypass the contract. Response
    validation is on by default (the conformance suite relies on it).
    """

    def __init__(
        self,
        tools: Any,
        *,
        catalog: ToolCatalog | None = None,
        authorizer: Any | None = None,
        validate_responses: bool = True,
    ) -> None:
        self._tools = tools
        self._catalog = catalog or default_catalog()
        self._validator = SchemaValidator(self._catalog)
        self._authorizer = authorizer
        self._validate_responses = validate_responses

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._catalog.get(name) is None:
            raise MethodNotFoundError(f"unknown tool: {name}", data={"tool": name})
        self._validator.validate_request(name, arguments)
        if self._authorizer is not None:
            await self._authorizer.authorize(name, arguments)
        result = await self._tools.dispatch(name, arguments)
        payload = result.model_dump(mode="json")
        if self._validate_responses:
            self._validator.validate_response(name, payload)
        return payload


class SessionTransport:
    """Call tools over an already-connected ``mcp.ClientSession`` (remote canon).

    Translates a typed call into ``session.call_tool`` and unwraps the result's
    ``structuredContent``. An ``isError`` result is raised as a typed
    :class:`MCPError` reconstructed from the wire body.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            body = _error_from_content(result)
            raise MCPError(body.get("message", "tool error"), data=body.get("data"))
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return dict(structured)
        # Fall back to the first JSON text block when no structuredContent.
        return _first_json_block(result)


def _error_from_content(result: Any) -> dict[str, Any]:
    import json

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {"message": text}
        if isinstance(data, dict):
            return data
    return {"message": "unknown tool error"}


def _first_json_block(result: Any) -> dict[str, Any]:
    import json

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


class KinoraMCPClient:
    """A typed client over the canon MCP tool surface.

    ``call`` is the generic entry; the per-tool convenience methods give callers
    a typed signature (the input model in, the output model out). A version pin
    may be attached per call and is carried in the ``_meta`` block.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        catalog: ToolCatalog | None = None,
    ) -> None:
        self._transport = transport
        self._catalog = catalog or default_catalog()

    @classmethod
    def in_process(
        cls,
        tools: Any,
        *,
        authorizer: Any | None = None,
        catalog: ToolCatalog | None = None,
        validate_responses: bool = True,
    ) -> KinoraMCPClient:
        """A client backed by direct in-process dispatch."""
        cat = catalog or default_catalog()
        return cls(
            InProcessTransport(
                tools,
                catalog=cat,
                authorizer=authorizer,
                validate_responses=validate_responses,
            ),
            catalog=cat,
        )

    @classmethod
    def over_session(cls, session: Any, *, catalog: ToolCatalog | None = None) -> KinoraMCPClient:
        """A client backed by a connected ``mcp.ClientSession`` (remote canon)."""
        return cls(SessionTransport(session), catalog=catalog)

    # --- generic --------------------------------------------------------------

    def meta(self, name: str) -> ToolMeta:
        """The catalog metadata for ``name`` (raises :class:`KeyError`)."""
        return self._catalog.require(name)

    async def call(
        self,
        name: str,
        arguments: BaseModel | dict[str, Any],
        *,
        version: str | ToolVersion | None = None,
    ) -> dict[str, Any]:
        """Call ``name`` with ``arguments`` (a model or dict); return the raw result.

        A ``version`` pin (when set) is carried in ``_meta`` so the server can
        reject an incompatible served version.
        """
        if isinstance(arguments, BaseModel):
            payload: dict[str, Any] = arguments.model_dump(mode="json", exclude_none=False)
        else:
            payload = dict(arguments)
        if version is not None:
            payload = {**payload, "_meta": {VERSION_META_KEY: str(version)}}
        return await self._transport.call(name, payload)

    async def call_typed(
        self,
        name: str,
        arguments: BaseModel | dict[str, Any],
        *,
        version: str | ToolVersion | None = None,
    ) -> BaseModel:
        """Call ``name`` and parse the result into the tool's typed output model."""
        raw = await self.call(name, arguments, version=version)
        meta = self._catalog.require(name)
        if meta.output_model is None:  # pragma: no cover - every tool has one today
            raise MCPError(f"tool {name!r} has no declared output model")
        return meta.output_model.model_validate(raw)

    # --- a few typed convenience wrappers over the hottest tools --------------

    async def canon_query(self, **kwargs: Any) -> BaseModel:
        """``canon.query`` — the retrieval policy for one beat (§8.4)."""
        from app.mcp import schemas

        return await self.call_typed("canon.query", schemas.CanonQueryInput(**kwargs))

    async def shot_render(self, **kwargs: Any) -> BaseModel:
        """``shot.render`` — cache-first, budget-gated enqueue (§8.7)."""
        from app.mcp import schemas

        return await self.call_typed("shot.render", schemas.ShotRenderInput(**kwargs))

    async def budget_remaining(self) -> BaseModel:
        """``budget.remaining`` — the guardrail snapshot (§11)."""
        from app.mcp import schemas

        return await self.call_typed("budget.remaining", schemas.BudgetRemainingInput())

    async def episodic_search(self, **kwargs: Any) -> BaseModel:
        """``episodic.search`` — nearest prior accepted shots (§8.2)."""
        from app.mcp import schemas

        return await self.call_typed("episodic.search", schemas.EpisodicSearchInput(**kwargs))

    async def prefs_get(self, **kwargs: Any) -> BaseModel:
        """``prefs.get`` — aggregated director priors for a scope (§8.6)."""
        from app.mcp import schemas

        return await self.call_typed("prefs.get", schemas.PrefsGetInput(**kwargs))

    async def safe_call(
        self, name: str, arguments: BaseModel | dict[str, Any]
    ) -> dict[str, Any]:
        """Call ``name``, returning the result or a typed error body (never raises).

        Useful for a UI / batch caller that wants to branch on the error category
        rather than catch exceptions.
        """
        try:
            return await self.call(name, arguments)
        except BaseException as exc:  # noqa: BLE001 - deliberately total
            return to_error_body(exc).to_dict()


__all__ = [
    "InProcessTransport",
    "KinoraMCPClient",
    "SessionTransport",
    "Transport",
]
