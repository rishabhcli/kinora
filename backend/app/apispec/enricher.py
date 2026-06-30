"""OpenAPI **enricher**: completeness without touching runtime responses (§5.6/§12).

FastAPI auto-generates an OpenAPI document, but the bare output is *thin* for a
public contract:

* operationIds are the verbose, position-coupled defaults
  (``get_engagement_api_analytics_engagement_get``) — they churn whenever a
  function is renamed and read poorly in a generated client;
* the typed ``{"error": {type, message, detail?}}`` envelope every handler
  returns on failure (see :mod:`app.api.errors`) is **undocumented** — clients
  only ever see the implicit ``422`` FastAPI adds for validation;
* there is no ``servers`` list, no contact/license metadata, no tag
  descriptions, and no per-status examples.

:func:`enrich_openapi` rewrites the generated spec dict in place to fix all of
that. It is a *pure metadata* transform applied through FastAPI's
``app.openapi`` override hook (:func:`install`): the path operations, parameters,
request bodies, and **success response schemas the renderer parses are never
altered** — only operationIds are normalised, error responses are *added*, and
``info``/``servers``/``tags`` are filled in. Nothing here runs at request time,
so no endpoint's actual response can change.

The transform is deterministic: given the same routes it produces byte-stable
operationIds and the same component schemas, which is what makes the golden-spec
diff gate (:mod:`app.apispec.diff`) meaningful.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.apispec.settings import ApiSpecSettings, get_apispec_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The HTTP methods that carry an OpenAPI *operation* object.
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: A short, human verb for each method, used to synthesise a missing summary.
_METHOD_VERB = {
    "get": "Get",
    "post": "Create",
    "put": "Replace",
    "patch": "Update",
    "delete": "Delete",
    "head": "Head",
    "options": "Options",
}

#: The reference to the shared error envelope schema (``ErrorResponse``). Every
#: handler renders failures as this shape (:mod:`app.api.errors`), so documenting
#: it on the error statuses is exact, not aspirational.
_ERROR_REF = "#/components/schemas/ErrorResponse"

#: The status codes the gateway can return as the typed error envelope, mapped to
#: the typed-error contract (the ``error.type`` string) the renderer can switch
#: on. Sourced 1:1 from :func:`app.api.errors.install_exception_handlers`.
_ERROR_STATUSES: dict[str, tuple[str, str]] = {
    "400": ("bad_request", "Malformed or rejected request."),
    "401": ("unauthorized", "Missing or invalid bearer token."),
    "402": ("budget_exceeded", "The hard video-budget cap was reached (§11)."),
    "403": ("forbidden", "Authenticated but not permitted."),
    "404": ("not_found", "The addressed resource does not exist."),
    "409": ("conflict", "A state conflict (e.g. live video gated off, §10)."),
    "422": ("validation_error", "Request body/params failed validation."),
    "429": ("rate_limited", "Rate limit or connection cap exceeded."),
    "500": ("internal_error", "Unexpected server error (scrubbed in prod)."),
    "502": ("provider_error", "An upstream model provider failed (§9)."),
}

#: An example error envelope rendered into each documented error response so a
#: client author sees the exact JSON shape they must parse.
_ERROR_EXAMPLE: dict[str, Any] = {
    "error": {
        "type": "validation_error",
        "message": "request validation failed",
        "detail": {"errors": [{"loc": ["body", "email"], "msg": "field required"}]},
    }
}

#: Per-tag descriptions — turns the flat tag list into a navigable doc. Tags not
#: listed here still appear; they just carry no prose (and are reported by the
#: completeness audit so the gap is visible, never silently swallowed).
_TAG_DESCRIPTIONS: dict[str, str] = {
    "meta": "Liveness, readiness, metrics, and the service root index.",
    "auth": "Registration, login, token issuance, and API-key management.",
    "books": "Upload/ingest, the shelf, rendered pages, canon graph, shot timeline.",
    "sessions": "Generation-on-scroll control: intent, seek, live event stream (§4).",
    "director": "Director tools: region comments, canon edits, conflict resolution (§5.4).",
    "films": "Per-scene assembled films and book-level event streams.",
    "library": "The reader's library catalog projection.",
    "prefs": "Learned directing-style priors and preference reset (§8.6).",
    "events": "Server-sent reading-room event streams.",
    "metrics": "Operator metrics surface.",
    "notifications": "Notification + webhook delivery platform (§5/§12).",
    "analytics": "Product-analytics event pipeline and query surface.",
    "assistant": "Grounded, spoiler-aware reader Q&A over the book (§8).",
    "finops": "Spend, budget, and cost-attribution reporting (§11).",
    "billing": "Billing, plans, and payment lifecycle.",
    "flags": "Feature flags and experimentation.",
    "recommendations": "Watch-next recommendations and interaction logging.",
    "reports": "Operator report generation and signed retrieval.",
    "integrations": "Third-party source import (Notion, Pocket, Readwise, …).",
    "workspaces": "Workspaces, teams, and collaboration ownership (§5).",
    "search": "Server-side search, suggestions, and reindex.",
    "translation": "Content translation subsystem (§8/§9).",
    "moderation": "Content moderation and safety operations (§9/§10).",
    "compliance": "Consent, retention, DSAR, legal holds, and the audit ledger.",
    "llmops": "Prompt registry, evaluation, and guardrails (gated).",
    "portability": "Data export/import and account/book/canon backup.",
    "media": "The media asset registry.",
    "plugins": "The sandboxed plugin/extension marketplace and dispatch.",
    "graphql": "The public GraphQL gateway (separate from the REST API).",
    "eval": "Model/agent evaluation surface.",
    "optim": "Scheduler/render optimisation controls.",
}


def _camel(parts: list[str]) -> str:
    """Join ``parts`` into a lowerCamelCase token (deterministic, ASCII-only)."""
    cleaned = [re.sub(r"[^a-zA-Z0-9]", "", p) for p in parts if p]
    cleaned = [p for p in cleaned if p]
    if not cleaned:
        return "op"
    head = cleaned[0]
    head = head[:1].lower() + head[1:]
    tail = "".join(p[:1].upper() + p[1:] for p in cleaned[1:])
    return head + tail


def _path_tokens(path: str) -> tuple[list[str], list[str]]:
    """Split a route path into (static-segment tokens, path-param tokens)."""
    statics: list[str] = []
    params: list[str] = []
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        m = re.fullmatch(r"\{(.+?)\}", seg)
        if m:
            params.append(m.group(1))
        else:
            statics.append(seg)
    return statics, params


def stable_operation_id(method: str, path: str, tag: str | None) -> str:
    """Compute a stable, readable operationId from method + path (+ leading tag).

    Deterministic and decoupled from Python function names so a refactor that
    renames a handler does not churn the published contract. The method maps to a
    semantic verb (``post`` → ``create``) and each ``{param}`` becomes ``ByParam``.
    Examples:

    * ``GET  /api/books``                       → ``booksGetBooks``
    * ``POST /api/sessions/{session_id}/intent``→ ``sessionsCreateSessionsBySessionIdIntent``
    * ``GET  /api/books/{book_id}/pages/{n}``   → ``getBooksByBookIdPagesByN`` (tagless)

    The leading tag groups operations in generated clients (``api.books.*``); the
    rest encodes the verb + path so two routes can never collide.
    """
    statics, params = _path_tokens(path)
    # Drop the version prefix so ids read against the resource, not "/api".
    if statics and statics[0] == "api":
        statics = statics[1:]
    verb = _METHOD_VERB.get(method.lower(), method.capitalize())
    # Interleave statics and "ByParam" markers preserving path order.
    ordered: list[str] = []
    s_iter = iter(statics)
    # Reconstruct order by walking the original path again.
    work = path.strip("/").split("/")
    if work and work[0] == "api":
        work = work[1:]
    for seg in work:
        m = re.fullmatch(r"\{(.+?)\}", seg)
        if m:
            ordered.append("By" + "".join(w.capitalize() for w in re.split(r"[_\-]", m.group(1))))
        elif seg:
            ordered.append(seg)
    parts: list[str] = []
    if tag:
        parts.append(tag)
    parts.append(verb.lower())
    parts.extend(ordered)
    # ``statics`` / ``params`` only consumed to validate the split above.
    _ = (statics, params, s_iter)
    return _camel(parts)


def _ensure_error_schema(components: dict[str, Any]) -> None:
    """Guarantee the shared ``ErrorResponse``/``ErrorBody`` schemas are present.

    When any endpoint declares ``ErrorResponse`` as a response model FastAPI
    already emits these; but most handlers raise typed errors rather than
    *return* them, so the schema may be absent. We add an explicit, exact copy of
    :class:`app.api.schemas.ErrorResponse` so the ``$ref`` we attach resolves.
    """
    schemas = components.setdefault("schemas", {})
    if "ErrorBody" not in schemas:
        schemas["ErrorBody"] = {
            "type": "object",
            "title": "ErrorBody",
            "description": "A typed error payload (never leaks secrets/stack traces).",
            "properties": {
                "type": {"type": "string", "title": "Type"},
                "message": {"type": "string", "title": "Message"},
                "detail": {
                    "anyOf": [{"type": "object"}, {"type": "null"}],
                    "title": "Detail",
                    "default": None,
                },
            },
            "required": ["type", "message"],
        }
    if "ErrorResponse" not in schemas:
        schemas["ErrorResponse"] = {
            "type": "object",
            "title": "ErrorResponse",
            "description": "The envelope every error response uses.",
            "properties": {"error": {"$ref": "#/components/schemas/ErrorBody"}},
            "required": ["error"],
        }


def _error_responses_for(method: str, has_auth: bool, has_body: bool) -> dict[str, Any]:
    """Build the documented error responses appropriate to one operation.

    We never blanket-attach all statuses — only the ones an operation can
    actually emit: ``401`` when it is authenticated, ``422`` when it has a
    validatable body/params, plus the always-possible ``429``/``500``. Mutating
    verbs additionally document ``402``/``409``/``502`` (budget/gate/provider).
    """
    codes: list[str] = ["429", "500"]
    if has_auth:
        codes.insert(0, "401")
    if has_body or method.lower() == "get":
        codes.append("422")
    if method.lower() in {"post", "put", "patch", "delete"}:
        codes.extend(["402", "409", "502"])
    out: dict[str, Any] = {}
    for code in codes:
        type_, summary = _ERROR_STATUSES[code]
        out[code] = {
            "description": f"{summary} (error.type=``{type_}``).",
            "content": {
                "application/json": {
                    "schema": {"$ref": _ERROR_REF},
                    "example": _ERROR_EXAMPLE,
                }
            },
        }
    return out


def enrich_openapi(
    spec: dict[str, Any], *, settings: ApiSpecSettings | None = None
) -> dict[str, Any]:
    """Return ``spec`` enriched in place (operationIds, errors, servers, info).

    Pure metadata transform — see the module docstring. Idempotent: running it
    twice yields the same document (operationIds are recomputed from path+method,
    error responses keyed by status are overwritten with the same content).
    """
    settings = settings or get_apispec_settings()
    paths: dict[str, Any] = spec.get("paths", {})
    components: dict[str, Any] = spec.setdefault("components", {})
    _ensure_error_schema(components)

    used_tags: set[str] = set()
    seen_ids: dict[str, str] = {}

    for path, item in paths.items():
        for method in _HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            tag = tags[0] if tags else None
            used_tags.update(tags)

            # 1) Stable operationId (collision-guarded — never silently overwrite).
            op_id = stable_operation_id(method, path, tag)
            if op_id in seen_ids and seen_ids[op_id] != f"{method} {path}":
                op_id = _camel([op_id, method])
            seen_ids[op_id] = f"{method} {path}"
            op["operationId"] = op_id

            # 2) Backfill a summary if FastAPI left it empty.
            if not op.get("summary"):
                verb = _METHOD_VERB.get(method.lower(), method.upper())
                op["summary"] = f"{verb} {path}"

            # 3) Document the typed error envelope on the statuses this op can emit.
            has_auth = bool(op.get("security")) or _operation_is_secured(op, spec)
            has_body = "requestBody" in op
            responses = op.setdefault("responses", {})
            for code, payload in _error_responses_for(method, has_auth, has_body).items():
                # Don't clobber an explicitly-declared response model for this code.
                if code not in responses:
                    responses[code] = payload

    _enrich_info(spec, settings)
    _enrich_servers(spec, settings)
    _enrich_security(spec)
    _enrich_tags(spec, used_tags)
    return spec


def _operation_is_secured(op: dict[str, Any], spec: dict[str, Any]) -> bool:
    """True when the operation is covered by a security requirement.

    An op is secured if it declares its own non-empty ``security`` *or* the spec
    has a global ``security`` and the op did not opt out with ``security: []``.
    """
    if "security" in op:
        return bool(op["security"])
    return bool(spec.get("security"))


def _enrich_info(spec: dict[str, Any], settings: ApiSpecSettings) -> None:
    info = spec.setdefault("info", {})
    info.setdefault(
        "description",
        "Kinora — turn a book into a page-synced film generated a few seconds "
        "ahead of the reader. This is the showrunner gateway: auth, library + "
        "ingest, the generation-on-scroll session control surface, the Director "
        "tools, and the realtime event streams. All failures share one typed "
        "`{error:{type,message,detail?}}` envelope; switch on `error.type`.",
    )
    info["contact"] = {"name": settings.contact_name, "url": settings.contact_url}
    info["license"] = {"name": settings.license_name}


def _enrich_servers(spec: dict[str, Any], settings: ApiSpecSettings) -> None:
    servers: list[dict[str, str]] = [
        {"url": settings.public_server_url, "description": settings.public_server_description}
    ]
    if settings.include_local_server and settings.local_server_url != settings.public_server_url:
        servers.append({"url": settings.local_server_url, "description": "Local development"})
    spec["servers"] = servers


def _enrich_security(spec: dict[str, Any]) -> None:
    """Normalise the bearer security scheme name + describe it.

    FastAPI names the ``HTTPBearer`` dependency-derived scheme ``HTTPBearer``; we
    keep that name (renaming would break the per-op ``security`` references) but
    attach a description + ``bearerFormat`` so generated clients know it carries
    a JWT.
    """
    schemes = spec.setdefault("components", {}).setdefault("securitySchemes", {})
    bearer = schemes.get("HTTPBearer")
    if isinstance(bearer, dict):
        bearer.setdefault("type", "http")
        bearer.setdefault("scheme", "bearer")
        bearer["bearerFormat"] = "JWT"
        bearer["description"] = (
            "A JWT access token from `POST /api/auth/login`, sent as "
            "`Authorization: Bearer <token>`."
        )


def _enrich_tags(spec: dict[str, Any], used_tags: set[str]) -> None:
    """Emit a top-level ``tags`` array with descriptions for navigability."""
    existing = {t.get("name"): t for t in spec.get("tags", []) if isinstance(t, dict)}
    out: list[dict[str, str]] = []
    for name in sorted(used_tags):
        entry = existing.get(name, {"name": name})
        if name in _TAG_DESCRIPTIONS:
            entry.setdefault("description", _TAG_DESCRIPTIONS[name])
        out.append(entry)
    spec["tags"] = out


def build_enriched_spec(app: FastAPI, *, settings: ApiSpecSettings | None = None) -> dict[str, Any]:
    """Generate the app's base OpenAPI, then return an enriched copy.

    Does not install the hook; callers (the diff tool, the generator, tests) use
    this to get the enriched document on demand without mutating the live app.
    """
    from fastapi.openapi.utils import get_openapi

    base = get_openapi(
        title=app.title,
        version=app.version,
        summary=getattr(app, "summary", None),
        description=app.description,
        routes=app.routes,
    )
    return enrich_openapi(base, settings=settings)


def install(app: FastAPI, *, settings: ApiSpecSettings | None = None) -> None:
    """Install the enriched ``custom_openapi`` hook on ``app`` (caches the result).

    Overrides ``app.openapi`` so ``/openapi.json`` and ``/docs`` serve the
    enriched document, while every *runtime* response stays exactly as the route
    handlers produce it. Safe to call once at startup; the generated spec is
    cached on ``app.openapi_schema`` per FastAPI's own convention.
    """
    settings = settings or get_apispec_settings()

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        app.openapi_schema = build_enriched_spec(app, settings=settings)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


__all__ = [
    "build_enriched_spec",
    "enrich_openapi",
    "install",
    "stable_operation_id",
]
