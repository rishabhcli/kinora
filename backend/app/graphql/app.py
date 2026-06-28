"""The mountable HTTP surface for the public GraphQL gateway.

Exposes the gateway as its **own** router (mounted by the app under ``/graphql``),
strictly additive to the existing REST routes:

* ``POST /graphql`` — execute a query/mutation (JSON body or persisted-query id);
* ``GET  /graphql`` — a tiny built-in GraphiQL-style playground (HTML);
* ``GET  /graphql/schema`` — the SDL export (``text/plain``);
* ``GET  /graphql/sdk`` — the generated TypeScript client SDK (``text/plain``);
* ``GET  /graphql/version`` — the API version + deprecation policy (JSON);
* ``GET  /graphql/stream`` — the SSE subscription bridge (§5.6 events);
* ``POST /graphql/keys`` / ``GET /graphql/keys`` / ``DELETE /graphql/keys/{id}`` —
  API-key admin, authenticated by the desktop **user JWT** (so a logged-in user
  mints scoped, revocable keys for third-party integrations).

API-key auth is applied to the execution endpoints; the key-admin endpoints use
the existing JWT verification (``app/api/security.py``). The container is read off
``request.app.state`` (the main app), so this stays a plain router with no
separate ASGI lifespan.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from app.graphql.auth import ApiKeyRecord, ApiKeyStore, RateLimiter, Scope, extract_key
from app.graphql.context import build_context
from app.graphql.errors import GraphQLError, mask_error, unauthenticated
from app.graphql.gateway import GraphQLRequest, run_graphql
from app.graphql.introspection import print_schema
from app.graphql.persisted import PersistedQueryStore
from app.graphql.root import get_schema
from app.graphql.sdk import generate_typescript_sdk
from app.graphql.versioning import api_version_payload

router = APIRouter(prefix="/graphql", tags=["graphql"])

# Process-wide stores keyed by container id (a container is per-app; tests build a
# fresh one). Keeping the persisted-query LRU per container avoids cross-app bleed.
_PERSISTED: dict[int, PersistedQueryStore] = {}


def _container(request: Request) -> Any:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - guards a misconfigured app
        raise GraphQLError("Application container is not initialized.")
    return container


def _persisted_store(container: Any) -> PersistedQueryStore:
    store = _PERSISTED.get(id(container))
    if store is None:
        store = PersistedQueryStore(container.redis)
        _PERSISTED[id(container)] = store
    return store


async def _authenticate(request: Request, container: Any) -> ApiKeyRecord:
    raw = extract_key(request.headers)
    if not raw:
        raise unauthenticated("Provide an API key via X-API-Key or Authorization: Bearer.")
    return await ApiKeyStore(container.redis).verify(raw)


# --------------------------------------------------------------------------- #
# Execution endpoints
# --------------------------------------------------------------------------- #


#: Cap on a batched request (an array body) to bound work per HTTP request.
MAX_BATCH = 25


@router.post("")
async def graphql_post(request: Request, body: Annotated[Any, Body(...)]) -> JSONResponse:
    """Execute a GraphQL operation, or an array of them (request batching).

    A single JSON object runs one operation; a JSON array (up to ``MAX_BATCH``)
    runs each operation against a *fresh* per-request context and returns an array
    of responses in the same order — fewer HTTP round-trips for a client issuing
    several independent operations.
    """
    container = _container(request)
    try:
        api_key = await _authenticate(request, container)
    except GraphQLError as exc:
        return JSONResponse({"errors": [mask_error(exc).to_dict()]}, status_code=200)

    schema = get_schema()
    persisted = _persisted_store(container)
    rate_limit = RateLimiter(container.redis)

    async def _run_one(raw: Any) -> dict[str, Any]:
        try:
            gql_request = GraphQLRequest.from_body(raw)
        except GraphQLError as exc:
            return {"errors": [exc.to_dict()]}
        # A fresh context (and dataloaders) per operation so a batch never shares
        # loader caches across independent operations.
        return await run_graphql(
            schema=schema,
            request=gql_request,
            context=build_context(container, api_key),
            persisted=persisted,
            rate_limit=rate_limit,
        )

    if isinstance(body, list):
        if not body:
            return JSONResponse(
                {"errors": [{"message": "A batched request must not be empty."}]},
                status_code=200,
            )
        if len(body) > MAX_BATCH:
            return JSONResponse(
                {"errors": [{"message": f"Batch exceeds the limit of {MAX_BATCH}."}]},
                status_code=200,
            )
        responses = [await _run_one(item) for item in body]
        return JSONResponse(responses, status_code=200)

    # GraphQL-over-HTTP: a well-formed single response is always HTTP 200.
    return JSONResponse(await _run_one(body), status_code=200)


@router.get("/stream")
async def graphql_subscription_stream(
    request: Request,
    sessionId: str = Query(...),  # noqa: N803 - GraphQL camelCase arg
    token: str | None = Query(default=None),
) -> StreamingResponse:
    """SSE subscription bridge for a session's §5.6 generation events.

    EventSource cannot set headers, so the API key may also be passed as
    ``?token=kinora_pk_…``. Scope + ownership are enforced before streaming.
    """
    container = _container(request)
    raw = extract_key(request.headers) or token
    from app.graphql.subscriptions import stream_session_events

    async def stream() -> Any:
        try:
            if not raw:
                raise unauthenticated("Provide an API key (header or ?token=).")
            api_key = await ApiKeyStore(container.redis).verify(raw)
            context = build_context(container, api_key)
            async for frame in stream_session_events(
                context, session_id=sessionId, response_key="sessionEvents"
            ):
                yield frame
        except GraphQLError as exc:
            err = mask_error(exc)
            yield f"event: error\ndata: {_json.dumps(err.to_dict())}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# --------------------------------------------------------------------------- #
# Schema / SDK / version (public, no auth — they describe the contract)
# --------------------------------------------------------------------------- #


@router.get("/schema", response_class=PlainTextResponse)
async def graphql_schema() -> PlainTextResponse:
    """The schema as an SDL document (for tooling, codegen, and diffing)."""
    return PlainTextResponse(print_schema(get_schema()), media_type="text/plain")


@router.get("/sdk", response_class=PlainTextResponse)
async def graphql_sdk() -> PlainTextResponse:
    """The generated TypeScript client SDK module."""
    return PlainTextResponse(
        generate_typescript_sdk(get_schema()),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/version")
async def graphql_version() -> JSONResponse:
    """The published API version, stability label, and deprecation policy."""
    return JSONResponse(api_version_payload())


@router.get("", response_class=HTMLResponse)
async def graphql_playground() -> HTMLResponse:
    """A tiny built-in playground (no external CDN) for exploring the API."""
    return HTMLResponse(_PLAYGROUND_HTML)


# --------------------------------------------------------------------------- #
# API-key administration (user-JWT authenticated)
# --------------------------------------------------------------------------- #


async def _jwt_user_id(request: Request, container: Any) -> str:
    """Resolve the user id from the desktop app's Bearer JWT (key-admin auth)."""
    from app.api.security import TokenError, decode_access_token

    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise unauthenticated("Key administration requires a user Bearer token.")
    token = header[7:].strip()
    try:
        claims = decode_access_token(token, container.settings)
    except TokenError as exc:
        raise unauthenticated("Invalid or expired token.") from exc
    return claims.sub


@router.post("/keys")
async def create_api_key(
    request: Request, body: Annotated[dict[str, Any], Body(default_factory=dict)]
) -> JSONResponse:
    """Mint a new scoped API key for the authenticated user (returns it once)."""
    container = _container(request)
    try:
        user_id = await _jwt_user_id(request, container)
    except GraphQLError as exc:
        return JSONResponse({"error": exc.to_dict()}, status_code=401)
    label = str(body.get("label") or "default")
    requested = body.get("scopes")
    scopes = tuple(requested) if isinstance(requested, list) and requested else Scope.READ_ONLY
    rpm = int(body.get("rpm") or 0) or None
    store = ApiKeyStore(container.redis)
    try:
        full_key, record = await store.create(
            user_id=user_id,
            scopes=scopes,
            label=label,
            rpm=rpm or 120,
        )
    except ValueError as exc:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=400)
    return JSONResponse({"apiKey": full_key, **record.public_view()}, status_code=201)


@router.get("/keys")
async def list_api_keys(request: Request) -> JSONResponse:
    """List the authenticated user's (redacted) API keys."""
    container = _container(request)
    try:
        user_id = await _jwt_user_id(request, container)
    except GraphQLError as exc:
        return JSONResponse({"error": exc.to_dict()}, status_code=401)
    records = await ApiKeyStore(container.redis).list_for_user(user_id)
    return JSONResponse({"keys": [r.public_view() for r in records]})


@router.delete("/keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request) -> JSONResponse:
    """Revoke one of the authenticated user's API keys."""
    container = _container(request)
    try:
        user_id = await _jwt_user_id(request, container)
    except GraphQLError as exc:
        return JSONResponse({"error": exc.to_dict()}, status_code=401)
    store = ApiKeyStore(container.redis)
    record = await store.get(key_id)
    if record is None or record.user_id != user_id:
        return JSONResponse({"error": {"message": "no such key"}}, status_code=404)
    await store.revoke(key_id)
    return JSONResponse({"revoked": True, "keyId": key_id})


_PLAYGROUND_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Kinora GraphQL</title>
<style>body{font-family:system-ui;margin:0;background:#0b0b10;color:#e8e8ef}
header{padding:14px 18px;border-bottom:1px solid #23232c;font-weight:600}
main{display:grid;grid-template-columns:1fr 1fr;gap:0;height:calc(100vh - 52px)}
textarea,pre{margin:0;padding:14px;border:0;font:13px/1.5 ui-monospace,monospace;
background:#0b0b10;color:#e8e8ef;resize:none}
textarea{border-right:1px solid #23232c}
.bar{padding:8px 14px;border-top:1px solid #23232c;display:flex;gap:8px;
align-items:center}
input{flex:1;padding:6px 8px;background:#15151c;border:1px solid #23232c;
color:#e8e8ef;border-radius:6px}
button{padding:6px 14px;background:#5b5bff;color:#fff;border:0;border-radius:6px;cursor:pointer}
</style></head><body>
<header>Kinora — public GraphQL API · <a href="/graphql/schema" style="color:#9b9bff">SDL</a>
 · <a href="/graphql/sdk" style="color:#9b9bff">TS SDK</a></header>
<div class="bar"><input id="key" placeholder="X-API-Key (kinora_pk_…)">
<button onclick="run()">Run ▶</button></div>
<main><textarea id="q">{ apiVersion { version stability } viewer { userId scopes } }</textarea>
<pre id="out">// result</pre></main>
<script>
async function run(){
 const r=await fetch('/graphql',{method:'POST',headers:{'content-type':'application/json',
 'x-api-key':document.getElementById('key').value},
 body:JSON.stringify({query:document.getElementById('q').value})});
 document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2);
}
</script></body></html>"""


__all__ = ["router"]
