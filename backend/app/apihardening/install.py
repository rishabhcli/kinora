"""The single wiring entrypoint for the hardening layer (fully opt-in).

:func:`install` adds the chosen hardening middleware to an existing ``FastAPI``
app and (optionally) the problem+json handlers + OpenAPI customization. It is
**additive**: callers pass explicit flags for each concern, defaults are
conservative, and nothing here mutates existing routes or their response bodies.

Middleware ordering note: Starlette runs ``add_middleware`` in *reverse* of the
order added (last-added wraps outermost). We add request-id last so it is the
outermost layer — every other middleware's response (including a rate-limit 429)
gets the request-id header and shares the logging-bound id.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app.apihardening.config import HardeningConfig
from app.apihardening.idempotency import (
    IdempotencyMiddleware,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from app.apihardening.openapi import install_openapi
from app.apihardening.problem import install_problem_handlers
from app.apihardening.ratelimit import (
    InMemoryTokenBucketStore,
    RateLimitMiddleware,
    RateLimitRule,
    RedisTokenBucketStore,
    TokenBucketStore,
)
from app.apihardening.requestid import RequestIdMiddleware
from app.apihardening.validation import RequestLimitsMiddleware


def install(
    app: FastAPI,
    *,
    config: HardeningConfig | None = None,
    redis: Any = None,
    enable_request_id: bool = True,
    enable_request_limits: bool = True,
    enable_rate_limit: bool = True,
    enable_idempotency: bool = True,
    enable_problem_handlers: bool | None = None,
    enable_openapi: bool = True,
    rate_limit_rules: tuple[RateLimitRule, ...] = (),
    rate_limit_store: TokenBucketStore | None = None,
    idempotency_store: IdempotencyStore | None = None,
) -> HardeningConfig:
    """Wire the hardening layer onto ``app`` and return the effective config.

    When ``redis`` (an object exposing ``raw.eval`` / ``raw.set`` like
    :class:`app.redis.client.RedisClient`) is supplied, the rate-limit and
    idempotency stores default to their Redis-backed variants (shared across
    replicas); otherwise they default to the in-process stores. Passing explicit
    ``*_store`` objects overrides this.

    ``enable_problem_handlers`` defaults to ``config.problem_json_enabled`` so the
    error surface stays the legacy envelope unless problem+json is turned on.
    """
    cfg = config or HardeningConfig()

    if enable_problem_handlers is None:
        enable_problem_handlers = cfg.problem_json_enabled

    if enable_problem_handlers:
        install_problem_handlers(app, config=cfg)

    # --- middleware (added inner-first; request-id added last == outermost) ---
    if enable_idempotency:
        store = idempotency_store
        if store is None:
            store = (
                RedisIdempotencyStore(redis)
                if redis is not None
                else InMemoryIdempotencyStore()
            )
        app.add_middleware(IdempotencyMiddleware, store=store, config=cfg)

    if enable_rate_limit and cfg.rate_limit_enabled:
        rl_store = rate_limit_store
        if rl_store is None:
            rl_store = (
                RedisTokenBucketStore(redis)
                if redis is not None
                else InMemoryTokenBucketStore()
            )
        app.add_middleware(
            RateLimitMiddleware,
            store=rl_store,
            config=cfg,
            rules=rate_limit_rules,
        )

    if enable_request_limits:
        app.add_middleware(RequestLimitsMiddleware, config=cfg)

    if enable_request_id:
        app.add_middleware(RequestIdMiddleware, config=cfg)

    if enable_openapi:
        install_openapi(app, config=cfg)

    return cfg


__all__ = ["install"]
