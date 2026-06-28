"""Shared transport configuration + retry logic for the Kinora SDK.

Both the sync and async clients build httpx requests, attach the bearer token,
enforce a timeout, and retry idempotent/safe requests on 429/502/503/504 +
network errors with exponential backoff + jitter (honoring ``Retry-After``). The
pure decision functions live here so they are shared and unit-testable without
any I/O.
"""

from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass, field

DEFAULT_RETRY_STATUSES = (429, 502, 503, 504)
RETRY_BY_DEFAULT_METHODS = frozenset({"GET", "HEAD", "DELETE"})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Knobs for the retry behaviour."""

    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 10.0
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES


@dataclass(slots=True)
class TransportConfig:
    base_url: str
    api_prefix: str = "/api"
    timeout_s: float = 15.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    user_agent: str = "kinora-sdk-py/1.0.0"


def build_url(base_url: str, api_prefix: str, path: str) -> str:
    """Join base + api prefix + path, never double-prefixing."""
    base = base_url.rstrip("/")
    rel = path if path.startswith("/") else f"/{path}"
    rel = rel if rel.startswith(api_prefix) else f"{api_prefix}{rel}"
    return f"{base}{rel}"


def should_retry_method(method: str, retryable: bool | None) -> bool:
    """Whether a method is retried (explicit override wins)."""
    if retryable is not None:
        return retryable
    return method.upper() in RETRY_BY_DEFAULT_METHODS


def backoff_delay_s(attempt: int, policy: RetryPolicy, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff for ``attempt`` (1-based)."""
    exp = min(policy.max_delay_s, policy.base_delay_s * (2 ** (attempt - 1)))
    r = rng or random
    return r.uniform(0.0, exp)


def parse_retry_after(header: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP-date) into seconds."""
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        # Python 3.10+ raises ValueError (not None) on an unparseable date.
        parsed = email.utils.parsedate_to_datetime(header)
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    return max(0.0, parsed.timestamp() - time.time())


__all__ = [
    "DEFAULT_RETRY_STATUSES",
    "RetryPolicy",
    "TransportConfig",
    "backoff_delay_s",
    "build_url",
    "parse_retry_after",
    "should_retry_method",
]
