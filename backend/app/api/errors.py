"""Typed JSON error handling for the gateway (kinora.md §12).

Every failure leaves the API as a stable ``{"error": {type, message, detail?}}``
envelope (:class:`app.api.schemas.ErrorResponse`) with an appropriate status:

* :class:`APIError` — the gateway's own typed errors (401/403/404/409/429/...);
* ``RequestValidationError`` — 422 with the offending fields;
* :class:`~app.memory.budget_service.BudgetExceeded` — 402, the hard video cap;
* :class:`~app.providers.errors.LiveVideoDisabled` — 409, the deliberate gate;
* :class:`~app.providers.errors.ProviderError` — 502, an upstream model failure;
* anything else — 500, scrubbed to a generic message outside local dev so no
  stack trace or secret can leak in production.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.realtime.connections import ConnectionLimitExceeded
from app.api.schemas import ErrorBody, ErrorResponse
from app.core.config import get_settings
from app.core.logging import get_logger
from app.memory.budget_service import BudgetExceeded
from app.providers.errors import LiveVideoDisabled, ProviderError

logger = get_logger("app.api.errors")


class APIError(Exception):
    """A typed gateway error rendered as :class:`ErrorResponse` with a status code."""

    def __init__(
        self,
        type_: str,
        message: str,
        *,
        status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type_
        self.message = message
        self.status = status
        self.detail = detail


def _json(
    status: int, type_: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(type=type_, message=message, detail=detail))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def install_exception_handlers(app: FastAPI) -> None:
    """Register the typed JSON exception handlers on ``app``."""

    @app.exception_handler(APIError)
    async def _on_api_error(_: Request, exc: APIError) -> JSONResponse:
        return _json(exc.status, exc.type, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _on_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field errors are safe to return (they describe the client's own input).
        errors = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", "invalid")}
            for err in exc.errors()
        ]
        return _json(422, "validation_error", "request validation failed", {"errors": errors})

    @app.exception_handler(BudgetExceeded)
    async def _on_budget(_: Request, exc: BudgetExceeded) -> JSONResponse:
        return _json(
            402,
            "budget_exceeded",
            "video budget cap reached",
            {
                "scope": exc.scope,
                "requested": exc.requested,
                "used": exc.used,
                "cap": exc.cap,
            },
        )

    @app.exception_handler(LiveVideoDisabled)
    async def _on_live_disabled(_: Request, exc: LiveVideoDisabled) -> JSONResponse:
        return _json(
            409,
            "live_video_disabled",
            "live video generation is gated off; the keyframe ladder serves this shot",
        )

    @app.exception_handler(ProviderError)
    async def _on_provider(_: Request, exc: ProviderError) -> JSONResponse:
        # ProviderError messages never carry the API key (see its docstring).
        logger.warning("api.provider_error", error=str(exc))
        detail = {"code": exc.code} if exc.code else None
        return _json(502, "provider_error", exc.message, detail)

    @app.exception_handler(ConnectionLimitExceeded)
    async def _on_conn_limit(_: Request, exc: ConnectionLimitExceeded) -> JSONResponse:
        # The realtime layer refuses an over-cap connection on an HTTP route with a
        # 429 (the SSE/WS paths surface it as a typed event / close code instead).
        return _json(
            429,
            "connection_limit",
            "too many concurrent connections; close one and retry",
            {"scope": exc.scope, "limit": exc.limit},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _json(exc.status_code, "http_error", str(detail))

    @app.exception_handler(Exception)
    async def _on_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("api.unhandled_error")
        if get_settings().is_local:
            return _json(500, "internal_error", f"{type(exc).__name__}: {exc}")
        return _json(500, "internal_error", "internal server error")


__all__ = ["APIError", "install_exception_handlers"]
