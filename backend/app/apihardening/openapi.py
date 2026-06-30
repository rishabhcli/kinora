"""OpenAPI customization documenting the error envelope, auth, and standards.

:func:`install_openapi` swaps ``app.openapi`` for a cached builder that augments
the generated schema additively (it never removes anything FastAPI emits):

* registers the **Problem** schema (RFC-7807) and the legacy **ErrorResponse**
  envelope under ``components.schemas`` so both are documented;
* declares the **bearer** (JWT) and **API-key** security schemes and marks them
  as an optional global security requirement (a route's own dependency still
  enforces auth — this only documents it);
* attaches a set of **standard error responses** (400/401/403/404/409/422/429/
  500) to every operation that doesn't already document them, all pointing at the
  configured error schema;
* documents the hardening **headers** (``Idempotency-Key`` request header, the
  ``RateLimit-*`` / ``Retry-After`` / ``X-Request-ID`` response headers).

It is opt-in: only :func:`install` (or an explicit call) wires it, so the default
OpenAPI document is unchanged unless requested.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.apihardening.config import HardeningConfig

_PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Problem",
    "description": (
        "An RFC-7807 problem document with a stable `code` extension member. "
        "Emitted as `application/problem+json` when problem-json is enabled."
    ),
    "properties": {
        "type": {"type": "string", "format": "uri", "example": "https://kinora.dev/problems/rate_limited"},
        "title": {"type": "string", "example": "Too Many Requests"},
        "status": {"type": "integer", "example": 429},
        "code": {
            "type": "string",
            "description": "The stable machine code clients switch on.",
            "example": "rate_limited",
        },
        "detail": {"type": "string", "nullable": True},
        "instance": {"type": "string", "nullable": True},
        "request_id": {"type": "string", "nullable": True},
    },
    "required": ["title", "status", "code"],
    "additionalProperties": True,
}

_LEGACY_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "ErrorResponse",
    "description": "The legacy Kinora error envelope (`{error: {type, message, detail?}}`).",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "message": {"type": "string"},
                "detail": {"type": "object", "nullable": True, "additionalProperties": True},
            },
            "required": ["type", "message"],
        }
    },
    "required": ["error"],
}

#: status -> (component schema name decided at build time, human description)
_STANDARD_ERRORS: dict[str, str] = {
    "400": "Bad request (malformed input).",
    "401": "Authentication required or failed.",
    "403": "Authenticated but not permitted.",
    "404": "Resource not found.",
    "409": "Conflict (e.g. in-progress idempotent request).",
    "422": "Validation error.",
    "429": "Rate limit exceeded — see Retry-After.",
    "500": "Internal server error.",
}


def install_openapi(app: FastAPI, *, config: HardeningConfig | None = None) -> None:
    """Install the augmenting OpenAPI builder on ``app`` (idempotent, cached)."""
    cfg = config or HardeningConfig()
    error_schema_name = "Problem" if cfg.problem_json_enabled else "ErrorResponse"
    error_ref = {"$ref": f"#/components/schemas/{error_schema_name}"}
    error_media = "application/problem+json" if cfg.problem_json_enabled else "application/json"

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=getattr(app, "summary", None),
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})

        # --- schemas ---
        schemas = components.setdefault("schemas", {})
        schemas.setdefault("Problem", _PROBLEM_SCHEMA)
        schemas.setdefault("ErrorResponse", _LEGACY_ERROR_SCHEMA)

        # --- security schemes (documentation only) ---
        security = components.setdefault("securitySchemes", {})
        security.setdefault(
            "BearerAuth",
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "A Kinora access token issued by `/api/auth/login`.",
            },
        )
        security.setdefault(
            "ApiKeyAuth",
            {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "A server-to-server API key.",
            },
        )

        # --- standard error responses + hardening headers on every operation ---
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method.lower() not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "options",
                    "head",
                }:
                    continue
                responses = operation.setdefault("responses", {})
                for status, description in _STANDARD_ERRORS.items():
                    responses.setdefault(
                        status,
                        {
                            "description": description,
                            "content": {error_media: {"schema": error_ref}},
                        },
                    )
                # Idempotency-Key request header on POSTs.
                if method.lower() == "post":
                    params = operation.setdefault("parameters", [])
                    if not any(
                        p.get("name") == cfg.idempotency_header and p.get("in") == "header"
                        for p in params
                    ):
                        params.append(
                            {
                                "name": cfg.idempotency_header,
                                "in": "header",
                                "required": False,
                                "schema": {
                                    "type": "string",
                                    "maxLength": cfg.idempotency_key_max_len,
                                },
                                "description": (
                                    "Opt-in idempotency key: the same key replays the "
                                    "first response within the idempotency window."
                                ),
                            }
                        )

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


__all__ = ["install_openapi"]
