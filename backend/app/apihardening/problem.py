"""RFC-7807-style ``application/problem+json`` error envelope (additive).

This is a *parallel* error surface to the gateway's existing
``{"error": {type, message, detail?}}`` envelope (:mod:`app.api.errors`). It is
opt-in: by default the live app keeps the legacy envelope so the desktop renderer
(`apps/desktop/src/lib/api.ts`) sees no change. When ``problem_json_enabled`` is
set (per-app via :func:`install_problem_handlers`, or for a sub-app), failures
render as RFC-7807::

    {
      "type": "https://kinora.dev/problems/budget_exceeded",
      "title": "Video budget cap reached",
      "status": 402,
      "code": "budget_exceeded",          # the STABLE machine code
      "detail": "video budget cap reached",
      "instance": "<request path>",
      "request_id": "<correlation id>",
      ...extension members...
    }

The ``code`` member is the stable contract a client switches on; ``type`` is a
dereferenceable URI built from it. Domain errors are mapped to a stable code +
HTTP status in one table (:data:`PROBLEM_MAP`) so the mapping is auditable and
testable in isolation.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.apihardening.config import HardeningConfig
from app.apihardening.requestid import current_request_id
from app.core.logging import get_logger

logger = get_logger("app.apihardening.problem")

#: The IANA media type for RFC-7807 problem documents.
PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    """An RFC-7807 problem document with a stable ``code`` extension member.

    Unknown extension members are permitted (RFC-7807 allows arbitrary extension
    members), so domain detail (``scope``, ``cap``, field errors, …) rides along
    in the same flat object.
    """

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int = Field(ge=100, le=599)
    #: The stable machine code (the contract clients switch on).
    code: str
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None

    def to_response(self) -> JSONResponse:
        """Render this problem as an ``application/problem+json`` response."""
        return JSONResponse(
            status_code=self.status,
            content=self.model_dump(mode="json", exclude_none=True),
            media_type=PROBLEM_MEDIA_TYPE,
        )


class ProblemException(Exception):  # noqa: N818 - "Problem" is the RFC-7807 term, not an error suffix
    """Raise a typed problem directly from a handler (status + stable code).

    Mirrors :class:`app.api.errors.APIError`, but carries RFC-7807 semantics and
    arbitrary extension members. The installed handler renders it.
    """

    def __init__(
        self,
        code: str,
        title: str,
        *,
        status: int = 400,
        detail: str | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.code = code
        self.title = title
        self.status = status
        self.detail = detail
        self.extensions = extensions or {}


# --------------------------------------------------------------------------- #
# Stable domain-error -> (code, status, title) mapping
# --------------------------------------------------------------------------- #
#: Class-name -> (stable_code, http_status, human_title). Looked up by name so
#: this module never needs to import every domain-error class (and so a stub /
#: partial build still imports cleanly). The codes are a stable wire contract.
PROBLEM_MAP: dict[str, tuple[str, int, str]] = {
    "BudgetExceeded": ("budget_exceeded", 402, "Video budget cap reached"),
    "LiveVideoDisabled": ("live_video_disabled", 409, "Live video generation is gated off"),
    "ProviderError": ("provider_error", 502, "Upstream model provider failed"),
    "ConnectionLimitExceeded": ("connection_limit", 429, "Too many concurrent connections"),
    "LockNotAcquiredError": ("conflict", 409, "Resource is busy; retry shortly"),
}


def _type_uri(base: str, code: str) -> str:
    if not base:
        return "about:blank"
    return base.rstrip("/") + "/" + code


def _extension_members(exc: Exception) -> dict[str, Any]:
    """Pull safe, client-facing extension members off a known domain error."""
    name = type(exc).__name__
    if name == "BudgetExceeded":
        return {
            "scope": getattr(exc, "scope", None),
            "requested": getattr(exc, "requested", None),
            "used": getattr(exc, "used", None),
            "cap": getattr(exc, "cap", None),
        }
    if name == "ProviderError":
        code = getattr(exc, "code", None)
        return {"provider_code": code} if code else {}
    if name == "ConnectionLimitExceeded":
        return {
            "scope": getattr(exc, "scope", None),
            "limit": getattr(exc, "limit", None),
        }
    return {}


def _domain_message(exc: Exception) -> str:
    """Prefer a domain error's curated ``message`` over ``str(exc)``."""
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and message else str(exc)


def problem_response(
    *,
    code: str,
    title: str,
    status: int,
    detail: str | None = None,
    request: Request | None = None,
    config: HardeningConfig | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build an ``application/problem+json`` response from raw fields.

    The single rendering path used by both the exception handlers and any caller
    that wants to emit a problem directly. ``request``/``config`` fill ``instance``
    and ``request_id`` when available.
    """
    cfg = config or HardeningConfig()
    members: dict[str, Any] = {}
    for key, value in (extensions or {}).items():
        if value is not None:
            members[key] = value
    problem = Problem(
        type=_type_uri(cfg.problem_type_base, code),
        title=title,
        status=status,
        code=code,
        detail=detail,
        instance=str(request.url.path) if request is not None else None,
        request_id=current_request_id() if cfg.problem_include_request_id else None,
        **members,
    )
    return problem.to_response()


def map_exception(
    exc: Exception, *, config: HardeningConfig, request: Request | None = None
) -> JSONResponse:
    """Map any exception to a problem+json response using :data:`PROBLEM_MAP`.

    Falls through to a scrubbed 500 for unknown exceptions (never leaking a stack
    trace or secret unless ``expose_internal_errors`` is set).
    """
    name = type(exc).__name__

    # The gateway's own typed APIError carries its status/type/detail already.
    if name == "APIError":
        status = int(getattr(exc, "status", 400))
        code = str(getattr(exc, "type", "error"))
        detail = _domain_message(exc)
        extensions = getattr(exc, "detail", None) or {}
        return problem_response(
            code=code,
            title=code.replace("_", " ").title(),
            status=status,
            detail=detail,
            request=request,
            config=config,
            extensions=extensions if isinstance(extensions, dict) else {},
        )

    mapped = PROBLEM_MAP.get(name)
    if mapped is not None:
        code, status, title = mapped
        if name == "ProviderError":
            logger.warning("problem.provider_error", error=str(exc))
        return problem_response(
            code=code,
            title=title,
            status=status,
            detail=_domain_message(exc),
            request=request,
            config=config,
            extensions=_extension_members(exc),
        )

    # Unknown -> scrubbed 500.
    logger.exception("problem.unhandled_error")
    detail = f"{name}: {exc}" if config.expose_internal_errors else "internal server error"
    return problem_response(
        code="internal_error",
        title="Internal Server Error",
        status=500,
        detail=detail,
        request=request,
        config=config,
    )


def install_problem_handlers(app: FastAPI, *, config: HardeningConfig) -> None:
    """Register the RFC-7807 exception handlers on ``app`` (opt-in surface).

    Installs handlers for :class:`ProblemException`, ``RequestValidationError``,
    ``StarletteHTTPException`` (incl. 404), and the catch-all ``Exception``.
    Domain errors flow through the catch-all via :func:`map_exception`. This is
    additive: only call it when problem+json is desired (e.g. a hardened sub-app
    or when ``config.problem_json_enabled`` is true).
    """

    @app.exception_handler(ProblemException)
    async def _on_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return problem_response(
            code=exc.code,
            title=exc.title,
            status=exc.status,
            detail=exc.detail,
            request=request,
            config=config,
            extensions=exc.extensions,
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", "invalid")}
            for err in exc.errors()
        ]
        return problem_response(
            code="validation_error",
            title="Request validation failed",
            status=422,
            detail="one or more fields failed validation",
            request=request,
            config=config,
            extensions={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        code = "not_found" if exc.status_code == 404 else "http_error"
        title = "Not Found" if exc.status_code == 404 else "HTTP error"
        return problem_response(
            code=code,
            title=title,
            status=exc.status_code,
            detail=detail,
            request=request,
            config=config,
        )

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return map_exception(exc, config=config, request=request)


__all__ = [
    "PROBLEM_MAP",
    "PROBLEM_MEDIA_TYPE",
    "Problem",
    "ProblemException",
    "install_problem_handlers",
    "map_exception",
    "problem_response",
]
