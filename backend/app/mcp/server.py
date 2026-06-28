"""The MCP server — the §8.3 tool surface over the official Model Context Protocol.

This module assembles a **complete, spec-compliant** MCP server around the
single execution path :meth:`app.mcp.tools.MemoryTools.dispatch`. It never
re-implements a tool — every layer here (versioning, validation, capability
negotiation, resources, subscriptions, identity-scoping) wraps that one
dispatch.

Two builders, layered:

* :func:`build_server` — the *baseline* low-level :class:`~mcp.server.Server`:
  ``list_tools`` advertises every tool's input **and output** JSON Schema (and
  its Kinora version + scope as ``_meta``), and ``call_tool`` runs the optional
  authorizer → :meth:`MemoryTools.dispatch` → response validation. Kept
  backward-compatible (existing callers pass ``tools`` + ``authorizer``).
* :func:`build_protocol_server` — the *full* surface: everything ``build_server``
  does, **plus** resource list/read/subscribe handlers, capability
  advertisement (``resources.subscribe`` + ``tools.listChanged`` +
  ``experimental`` Kinora versioning/scopes), and change-notification plumbing.
  Returns a :class:`ProtocolServer` holding the SDK server + the catalog +
  validator + resource provider + subscription registry.

**Security (kinora.md §12).** The streamable-HTTP MCP is a control surface (it
can enqueue real renders that spend metered video-seconds), so it is *not*
exposed unauthenticated:

* when ``MCP_AUTH_TOKEN`` is set, :class:`BearerAuthMiddleware` requires a
  matching ``Authorization: Bearer <token>`` on every request (401 otherwise);
* outside ``local`` the token is **mandatory** — :func:`build_streamable_http_app`
  refuses to start the HTTP MCP without it.

Per-tool/per-client authorization (scope + book ownership) is the
:class:`ToolAuthorizer` seam — :class:`app.mcp.identity.ScopedAuthorizer`
composed with :class:`app.mcp.authz.BookScopedAuthorizer`.
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.mcp.capabilities import KINORA_EXPERIMENTAL_KEY, ServerCapabilities
from app.mcp.errors import MethodNotFoundError, VersionError, to_error_body
from app.mcp.registry import ToolCatalog, default_catalog
from app.mcp.resources import ResourceProvider, SubscriptionRegistry, resolve_uri
from app.mcp.tools import TOOL_DEFS
from app.mcp.validation import SchemaValidator
from mcp.server import Server

logger = get_logger("app.mcp.server")

DEFAULT_SERVER_NAME = "kinora-canon-memory"

#: The ``_meta`` key under which a call may pin a tool version (the Kinora ext).
VERSION_META_KEY = "io.kinora.canon/version"


class ToolDispatcher(Protocol):
    """The one method the protocol layer needs from the tool surface.

    :class:`~app.mcp.tools.MemoryTools` satisfies this structurally (it *is* the
    production implementation), but typing the server against the dispatch
    contract — not the concrete class — keeps the protocol layer decoupled from
    ``tools.py`` (the single execution path) and lets a test double stand in.
    """

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any: ...


class ToolAuthorizer(Protocol):
    """Per-call authorization seam for the MCP tool surface (kinora.md §12).

    Invoked before every tool dispatch with the tool name and its (validated)
    arguments — which include ``book_id`` for the book-scoped tools — so a real
    implementation can resolve the caller's identity and assert per-book
    ownership / per-client scope. Raise to deny; return to allow.
    """

    async def authorize(self, tool_name: str, arguments: dict[str, Any]) -> None: ...


def _tool_meta_block(catalog: ToolCatalog, name: str) -> dict[str, Any]:
    """The ``_meta`` block advertised on a tool (its Kinora version + scopes)."""
    meta = catalog.get(name)
    if meta is None:
        return {}
    return {
        KINORA_EXPERIMENTAL_KEY: {
            "version": str(meta.version),
            "scopes": sorted(s.value for s in meta.scopes),
            "bookScoped": meta.book_scoped,
        }
    }


def _strip_version_pin(arguments: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Pull an optional ``_meta`` version pin out of the call arguments.

    A client pins a tool version by passing ``{"_meta": {VERSION_META_KEY: "1.0"}}``
    alongside the real args. The pin is removed before validation/dispatch so the
    tool's input model never sees it.
    """
    if "_meta" not in arguments:
        return arguments, None
    args = dict(arguments)
    meta = args.pop("_meta") or {}
    version = meta.get(VERSION_META_KEY) if isinstance(meta, dict) else None
    return args, (str(version) if version is not None else None)


def _resolve_version_or_raise(catalog: ToolCatalog, name: str, pin: str | None) -> None:
    """Validate a pinned tool version, mapping a mismatch to a typed VersionError."""
    if pin is None:
        return
    try:
        catalog.resolve_version(name, pin)
    except ValueError as exc:
        raise VersionError(str(exc), data={"tool": name, "requested": pin}) from exc


def build_server(
    tools: ToolDispatcher,
    *,
    name: str = DEFAULT_SERVER_NAME,
    authorizer: ToolAuthorizer | None = None,
    catalog: ToolCatalog | None = None,
    validate_responses: bool = True,
) -> Server[Any, Any]:
    """Build an MCP :class:`Server` exposing the memory tools.

    ``authorizer`` (optional) is consulted before each dispatch — the per-client
    scope + per-book ownership seam (§12). ``validate_responses`` runs the
    handler's result against its declared output schema (a server-contract gate).
    Backward-compatible: the original two-argument call still works.
    """
    catalog = catalog or default_catalog()
    validator = SchemaValidator(catalog)
    server: Server[Any, Any] = Server(name)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        out: list[types.Tool] = []
        for defn in TOOL_DEFS:
            meta = catalog.get(defn.name)
            out.append(
                types.Tool(
                    name=defn.name,
                    description=defn.description,
                    inputSchema=defn.input_model.model_json_schema(),
                    outputSchema=(meta.output_schema() if meta else None),
                    _meta=_tool_meta_block(catalog, defn.name) or None,
                )
            )
        return out

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if catalog.get(name) is None:
            raise MethodNotFoundError(f"unknown tool: {name}", data={"tool": name})
        args, version_pin = _strip_version_pin(arguments)
        _resolve_version_or_raise(catalog, name, version_pin)
        validator.validate_request(name, args)
        if authorizer is not None:
            await authorizer.authorize(name, args)
        result = await tools.dispatch(name, args)
        payload = result.model_dump(mode="json")
        if validate_responses:
            validator.validate_response(name, payload)
        return payload

    return server


async def run_stdio(tools: ToolDispatcher, *, name: str = DEFAULT_SERVER_NAME) -> None:
    """Serve the memory tools over stdio (the canonical MCP transport)."""
    server = build_server(tools, name=name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# --------------------------------------------------------------------------- #
# The full protocol server: tools + resources + subscriptions + capabilities
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ProtocolServer:
    """The full MCP surface bundle (tools + resources + subscriptions).

    Holds the SDK :class:`Server` plus the Kinora protocol scaffolding so the
    transport layer (and the conformance suite) can reach the catalog, validator,
    resource provider, and subscription registry without rebuilding them.
    """

    server: Server[Any, Any]
    catalog: ToolCatalog
    validator: SchemaValidator
    resources: ResourceProvider
    subscriptions: SubscriptionRegistry
    capabilities: ServerCapabilities

    def initialization_options(self) -> Any:
        """Init options advertising the resource/tool/experimental capabilities.

        The SDK derives ``resources.subscribe`` as ``False`` (it has no
        ``NotificationOptions`` slot for it) even though we register a real
        ``subscribe_resource`` handler — so we overwrite the derived resources
        capability with our advertised one, which is the truthful surface
        (subscriptions genuinely work).
        """
        opts = self.server.create_initialization_options(
            notification_options=NotificationOptions(
                resources_changed=self.capabilities.resources.list_changed,
                tools_changed=self.capabilities.tools.list_changed,
            ),
            experimental_capabilities={
                KINORA_EXPERIMENTAL_KEY: self.capabilities.experimental.to_dict()
            },
        )
        if opts.capabilities.resources is not None:
            opts.capabilities.resources.subscribe = self.capabilities.resources.subscribe
        return opts

    async def notify_resource_updated(self, uri: str) -> int:
        """Send ``resources/updated`` to the current session if it watches ``uri``.

        Returns the number of notifications sent. Best-effort: with no request
        context (e.g. a direct unit-test call or a stateless transport) the SDK
        session lookup fails and the call is a safe no-op.
        """
        watchers = self.subscriptions.subscribers_for(uri)
        if not watchers:
            return 0
        try:
            session = self.server.request_context.session
        except LookupError:
            return 0
        sent = 0
        with contextlib.suppress(Exception):
            from pydantic import AnyUrl

            await session.send_resource_updated(AnyUrl(uri))
            sent = len(watchers)
        return sent

    async def fan_out_changes(self, tool_name: str, arguments: dict[str, Any]) -> list[str]:
        """Notify subscribers of every resource a completed write touched.

        Returns the touched URIs (for observability / tests).
        """
        touched = ResourceProvider.resources_touched_by(tool_name, arguments)
        for uri in touched:
            await self.notify_resource_updated(uri)
        return touched


def build_protocol_server(
    tools: ToolDispatcher,
    *,
    name: str = DEFAULT_SERVER_NAME,
    authorizer: ToolAuthorizer | None = None,
    catalog: ToolCatalog | None = None,
    subscriptions: SubscriptionRegistry | None = None,
    validate_responses: bool = True,
) -> ProtocolServer:
    """Build the full protocol server: tools + resources + subscriptions.

    Layers resource handlers and change notifications on top of the baseline
    tool surface. A write tool that mutates canon fans out a ``resources/updated``
    notification to every subscribed client (§8 — the inspectable, *live* canon).
    """
    catalog = catalog or default_catalog()
    subscriptions = subscriptions or SubscriptionRegistry()
    resources = ResourceProvider(tools)
    validator = SchemaValidator(catalog)
    capabilities = ServerCapabilities.for_catalog(catalog)

    server: Server[Any, Any] = Server(name)
    bundle = ProtocolServer(
        server=server,
        catalog=catalog,
        validator=validator,
        resources=resources,
        subscriptions=subscriptions,
        capabilities=capabilities,
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=m.name,
                description=m.description,
                inputSchema=m.input_schema(),
                outputSchema=m.output_schema(),
                _meta=_tool_meta_block(catalog, m.name) or None,
            )
            for m in catalog.metas
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if catalog.get(name) is None:
            raise MethodNotFoundError(f"unknown tool: {name}", data={"tool": name})
        args, version_pin = _strip_version_pin(arguments)
        _resolve_version_or_raise(catalog, name, version_pin)
        validator.validate_request(name, args)
        if authorizer is not None:
            await authorizer.authorize(name, args)
        result = await tools.dispatch(name, args)
        payload = result.model_dump(mode="json")
        if validate_responses:
            validator.validate_response(name, payload)
        if (meta := catalog.get(name)) is not None and meta.is_write:
            await bundle.fan_out_changes(name, args)
        return payload

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        # Concrete resources are per-book (constructed from the templates below).
        # With no book in scope the top-level list is empty; registering the
        # handler is what makes the SDK advertise the ``resources`` capability.
        return []

    @server.list_resource_templates()
    async def _list_templates() -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                uriTemplate=t["uriTemplate"],
                name=t["name"],
                title=t["title"],
                description=t["description"],
                mimeType=t["mimeType"],
            )
            for t in resources.templates()
        ]

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        contents = await resources.read(str(uri))
        return contents.text

    @server.subscribe_resource()
    async def _subscribe(uri: Any) -> None:
        resolve_uri(str(uri))  # validate shape (raises on a bad URI)
        subscriptions.subscribe(_current_client_id(server), str(uri))

    @server.unsubscribe_resource()
    async def _unsubscribe(uri: Any) -> None:
        subscriptions.unsubscribe(_current_client_id(server), str(uri))

    return bundle


def _current_client_id(server: Server[Any, Any]) -> str:
    """A stable id for the current connection (the SDK session object identity).

    The streamable-HTTP manager gives each connection its own session; we key
    subscriptions on its object id so a write fans out to the right clients. When
    there is no request context (a direct unit-test call), fall back to a constant.
    """
    try:
        session = server.request_context.session
    except LookupError:
        return "default"
    return f"sess-{id(session)}"


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
    tools: ToolDispatcher,
    *,
    name: str = DEFAULT_SERVER_NAME,
    json_response: bool = True,
    settings: Settings | None = None,
    authorizer: ToolAuthorizer | None = None,
    identity_resolver: Any | None = None,
    stateful: bool | None = None,
    catalog: ToolCatalog | None = None,
) -> Any:
    """Wrap the server as a Starlette ASGI app for streamable-HTTP transport.

    Mounts the MCP endpoint at ``/mcp`` and gates it with
    :class:`BearerAuthMiddleware` when ``MCP_AUTH_TOKEN`` is configured. Outside
    ``local`` the token is mandatory: this refuses to start an unauthenticated
    control surface in any non-local environment (§12). Imported lazily so stdio
    deployments do not require Starlette at import time.

    When ``identity_resolver`` is given, an
    :class:`~app.mcp.identity.IdentityMiddleware` runs *inside* the bearer gate to
    resolve each request's caller (per-client scoping, §12); a
    :class:`~app.mcp.identity.ContextScopedAuthorizer` then reads that identity in
    the dispatch path.

    When ``stateful`` is True (or auto-enabled because the server advertises
    resource subscriptions) the manager keeps per-session state so resource
    subscriptions + change notifications work end-to-end; otherwise it runs the
    historical stateless mode.

    Raises:
        RuntimeError: when ``app_env`` is not ``local`` and ``MCP_AUTH_TOKEN`` is unset.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from app.mcp.identity import IdentityMiddleware

    settings = settings or get_settings()
    auth_token = settings.mcp_auth_token
    # The auth boundary is *either* the shared door token *or* a per-client token
    # table (the identity resolver) — a resolver rejects unrecognised tokens with
    # a 403, which is itself authentication. Outside ``local`` at least one must
    # be present (an unauthenticated control surface must never run in prod, §12).
    has_auth = bool(auth_token) or identity_resolver is not None
    if not has_auth and not settings.is_local:
        raise RuntimeError(
            "refusing to start the streamable-HTTP MCP without an auth boundary "
            f"(app_env={settings.app_env!r}): set MCP_AUTH_TOKEN or a per-client "
            "token table (MCP_CLIENT_SCOPES) — an unauthenticated control surface "
            "must not run outside 'local'"
        )

    bundle = build_protocol_server(tools, name=name, authorizer=authorizer, catalog=catalog)
    want_stateful = stateful if stateful is not None else bundle.capabilities.resources.subscribe
    manager = StreamableHTTPSessionManager(
        app=bundle.server, json_response=json_response, stateless=not want_stateful
    )

    async def handle(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    # Starlette runs the *last*-added middleware outermost. The identity resolver
    # is the per-client auth boundary; when a separate shared door token is also
    # set it wraps the resolver (coarse gate first, then per-client resolution).
    if identity_resolver is not None:
        app.add_middleware(IdentityMiddleware, resolver=identity_resolver)
        logger.info("mcp.http.identity_scoping_enabled", stateful=want_stateful)
    if auth_token:
        app.add_middleware(BearerAuthMiddleware, token=auth_token)
        logger.info("mcp.http.auth_enabled", stateful=want_stateful)
    elif identity_resolver is None:
        # Only reachable in local (the non-local case raised above).
        logger.warning("mcp.http.auth_disabled", env=settings.app_env)
    return app


# --------------------------------------------------------------------------- #
# Error rendering helpers (used by the typed client + conformance suite)
# --------------------------------------------------------------------------- #


def render_tool_error(exc: BaseException) -> dict[str, Any]:
    """Coerce any exception into the wire error body (typed category + code)."""
    return to_error_body(exc).to_dict()


def is_error_payload(payload: dict[str, Any]) -> bool:
    """True when a payload is a typed MCP error body."""
    return bool(payload.get("error")) and "category" in payload


__all__ = [
    "DEFAULT_SERVER_NAME",
    "VERSION_META_KEY",
    "BearerAuthMiddleware",
    "ProtocolServer",
    "ToolAuthorizer",
    "build_protocol_server",
    "build_server",
    "build_streamable_http_app",
    "is_error_payload",
    "render_tool_error",
    "run_stdio",
]
