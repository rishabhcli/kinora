"""Cross-cutting API hardening — additive middleware + reusable utilities.

This package is a self-contained, **opt-in** hardening layer for the Kinora
gateway. Nothing here is wired by default: :func:`app.main.create_app` (or any
alternate entrypoint) installs the pieces it wants via :func:`install`, and
existing routes keep their current request/response shapes untouched.

What it provides, all additively:

* **A typed problem+json envelope** (:mod:`.problem`) — an RFC-7807-style
  ``application/problem+json`` body with a *stable machine code* per failure,
  plus a global exception handler that maps Kinora's domain errors
  (:class:`~app.providers.errors.ProviderError`,
  :class:`~app.memory.budget_service.BudgetExceeded`, the gateway
  :class:`~app.api.errors.APIError`, …) onto it. The legacy
  ``{"error": {...}}`` envelope (:class:`app.api.schemas.ErrorResponse`) is left
  as the default so current clients (the desktop renderer) never see a changed
  shape; problem+json is requested per-app or per-route.
* **Idempotency-Key** support for unsafe POSTs (:mod:`.idempotency`): store the
  first response and replay it verbatim within a window, with an in-memory store
  and a Redis-interface store sharing one protocol.
* **Cursor pagination** helpers + a generic :class:`~.pagination.Page` model
  (:mod:`.pagination`): opaque, signed, tamper-evident cursors.
* **Request validation / limits** (:mod:`.validation`): body-size cap,
  content-type allow-list, returned as problem+json.
* **Request-id / correlation-id** propagation (:mod:`.requestid`).
* **A configurable token-bucket rate limiter** (:mod:`.ratelimit`): per
  principal/IP, ``429`` + ``Retry-After`` + IETF ``RateLimit-*`` headers,
  fail-open, in-memory or Redis-backed.
* **OpenAPI customization** (:mod:`.openapi`) documenting the problem envelope,
  the bearer/api-key security schemes, and the standard error responses.

See :func:`install` for the single wiring entrypoint.
"""

from __future__ import annotations

from app.apihardening.config import HardeningConfig
from app.apihardening.idempotency import (
    IdempotencyMiddleware,
    IdempotencyRecord,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from app.apihardening.install import install
from app.apihardening.pagination import (
    Cursor,
    CursorCodec,
    Page,
    PageMeta,
    decode_cursor,
    encode_cursor,
)
from app.apihardening.problem import (
    Problem,
    ProblemException,
    install_problem_handlers,
    problem_response,
)
from app.apihardening.ratelimit import (
    InMemoryTokenBucketStore,
    RateLimitMiddleware,
    RateLimitRule,
    RedisTokenBucketStore,
    TokenBucketStore,
)
from app.apihardening.requestid import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    current_request_id,
)
from app.apihardening.validation import RequestLimitsMiddleware

__all__ = [
    "REQUEST_ID_HEADER",
    "Cursor",
    "CursorCodec",
    "HardeningConfig",
    "IdempotencyMiddleware",
    "IdempotencyRecord",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "InMemoryTokenBucketStore",
    "Page",
    "PageMeta",
    "Problem",
    "ProblemException",
    "RateLimitMiddleware",
    "RateLimitRule",
    "RedisIdempotencyStore",
    "RedisTokenBucketStore",
    "RequestIdMiddleware",
    "RequestLimitsMiddleware",
    "TokenBucketStore",
    "current_request_id",
    "decode_cursor",
    "encode_cursor",
    "install",
    "install_problem_handlers",
    "problem_response",
]
