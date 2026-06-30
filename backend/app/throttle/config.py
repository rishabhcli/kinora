"""Declarative limit specs (pydantic v2) and the factory that builds a wired
:class:`~app.throttle.client.ThrottleClient` from them.

Application code shouldn't hand-assemble limiters and adapters; it should declare
*what* limits apply and let the factory wire the *how*. A :class:`LimitSpec` names
an algorithm + parameters + the hierarchy level it sits at; a :class:`FabricSpec`
is the ordered set of them plus an optional concurrency pool and the fail-open
choice. :func:`build_client` turns a spec into a ready client over a given
transport.

These are plain pydantic models (no env binding) so they can be embedded in the
backend :class:`~app.core.config.Settings` later or built ad-hoc per provider; the
throttle package stays import-light and does not pull settings at import time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.throttle.algorithms.gcra import GcraConfig, GcraLimiter
from app.throttle.algorithms.sliding_window import SlidingWindowConfig, SlidingWindowLimiter
from app.throttle.algorithms.token_bucket import TokenBucketConfig, TokenBucketLimiter
from app.throttle.client import ThrottleClient
from app.throttle.hierarchy import (
    GcraLimit,
    HierarchicalLimiter,
    Limit,
    SlidingWindowLimit,
    TokenBucketLimit,
)
from app.throttle.leases import ConcurrencyLeasePool, LeaseConfig
from app.throttle.transport import Transport


class Level(StrEnum):
    """Hierarchy levels, ordered broadest → narrowest.

    The factory sorts limits by this order so the hierarchy always checks broad
    limits first (cheapest denials, fewest rollbacks). The enum values double as
    the sort key via :attr:`order`.
    """

    GLOBAL = "global"
    PROVIDER = "provider"
    TENANT = "tenant"
    ENDPOINT = "endpoint"

    @property
    def order(self) -> int:
        return {
            Level.GLOBAL: 0,
            Level.PROVIDER: 1,
            Level.TENANT: 2,
            Level.ENDPOINT: 3,
        }[self]


class LimitSpec(BaseModel):
    """One declarative limit: an algorithm, its parameters, level, and scope key.

    Exactly the parameters for the chosen ``algorithm`` must be present; the
    validator enforces that so a misconfigured spec fails loudly at construction
    rather than silently using a default.
    """

    model_config = {"frozen": True}

    algorithm: Literal["token_bucket", "sliding_window", "gcra"]
    level: Level
    scope: str = Field(..., min_length=1)

    # token_bucket
    rate: float | None = Field(default=None, gt=0)
    capacity: float | None = Field(default=None, gt=0)
    # sliding_window
    limit: int | None = Field(default=None, gt=0)
    window_s: float | None = Field(default=None, gt=0)
    # gcra (reuses rate; burst optional)
    burst: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_params(self) -> LimitSpec:
        if self.algorithm == "token_bucket":
            if self.rate is None or self.capacity is None:
                raise ValueError("token_bucket needs rate and capacity")
        elif self.algorithm == "sliding_window":
            if self.limit is None or self.window_s is None:
                raise ValueError("sliding_window needs limit and window_s")
        elif self.algorithm == "gcra" and self.rate is None:
            raise ValueError("gcra needs rate")
        return self

    def build(self, transport: Transport, *, key_prefix: str) -> Limit:
        """Construct the concrete :class:`~app.throttle.hierarchy.Limit` adapter."""
        scoped = f"{self.level.value}:{self.scope}"
        if self.algorithm == "token_bucket":
            assert self.rate is not None and self.capacity is not None
            limiter = TokenBucketLimiter(
                transport,
                scoped,
                TokenBucketConfig(rate=self.rate, capacity=self.capacity),
                key_prefix=f"{key_prefix}:tb",
            )
            return TokenBucketLimit(limiter)
        if self.algorithm == "sliding_window":
            assert self.limit is not None and self.window_s is not None
            sw = SlidingWindowLimiter(
                transport,
                scoped,
                SlidingWindowConfig(limit=self.limit, window_s=self.window_s),
                key_prefix=f"{key_prefix}:sw",
            )
            return SlidingWindowLimit(sw)
        assert self.rate is not None
        g = GcraLimiter(
            transport,
            scoped,
            GcraConfig(rate=self.rate, burst=self.burst or 1),
            key_prefix=f"{key_prefix}:gcra",
        )
        return GcraLimit(g)


class LeaseSpec(BaseModel):
    """A fleet-wide concurrency cap to enforce alongside the rate hierarchy."""

    model_config = {"frozen": True}

    scope: str = Field(..., min_length=1)
    capacity: int = Field(..., ge=1)
    ttl_s: float = Field(..., gt=0)


class FabricSpec(BaseModel):
    """A complete throttle config: the limit set, optional lease pool, fail policy."""

    model_config = {"frozen": True}

    limits: list[LimitSpec] = Field(..., min_length=1)
    lease: LeaseSpec | None = None
    fail_open: bool = True
    key_prefix: str = "throttle"


def build_client(
    spec: FabricSpec,
    transport: Transport,
    **client_kwargs: object,
) -> ThrottleClient:
    """Build a wired :class:`ThrottleClient` from a :class:`FabricSpec`.

    Limits are sorted broadest→narrowest by :attr:`Level.order` so the hierarchy
    short-circuits and rolls back cheaply. Extra ``client_kwargs`` (e.g. a test
    ``clock``/``sleep``) pass through to the client.
    """
    ordered = sorted(spec.limits, key=lambda s: s.level.order)
    limits: list[Limit] = [s.build(transport, key_prefix=spec.key_prefix) for s in ordered]
    hierarchy = HierarchicalLimiter(limits)

    pool: ConcurrencyLeasePool | None = None
    if spec.lease is not None:
        pool = ConcurrencyLeasePool(
            transport,
            spec.lease.scope,
            LeaseConfig(capacity=spec.lease.capacity, ttl_s=spec.lease.ttl_s),
            key_prefix=f"{spec.key_prefix}:lease",
        )

    return ThrottleClient(
        hierarchy,
        lease_pool=pool,
        fail_open=spec.fail_open,
        **client_kwargs,  # type: ignore[arg-type]
    )


__all__ = [
    "FabricSpec",
    "LeaseSpec",
    "Level",
    "LimitSpec",
    "build_client",
]
