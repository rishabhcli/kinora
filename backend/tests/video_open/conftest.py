"""Shared fixtures + helpers for the open-video-adapter tests (fully mocked).

No network, no infra, deterministic. A :class:`RouteMap` lets a test describe a
provider's submit/poll/download responses by URL substring and assert how many
times each was hit. The :data:`FAST` resilience config + zero poll interval keep
the suite instantaneous.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from app.core.config import Settings
from app.providers.base import ResilienceConfig
from app.video.adapters.open.base import PollConfig

#: A mock-transport responder: maps a request to a response.
Responder = Callable[[httpx.Request], httpx.Response]

#: Resilience tuned so a unit test never sleeps on retries/rate-limits.
FAST = ResilienceConfig(
    max_attempts=2,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
    backoff_jitter_s=0.0,
    rate_per_s=10_000.0,
    rate_burst=10_000,
    default_timeout_s=5.0,
)

#: Poll config that never sleeps and times out fast.
NO_SLEEP_POLL = PollConfig(timeout_s=5.0, interval_s=0.0, max_interval_s=0.0, backoff=1.0)


@pytest.fixture
def settings() -> Settings:
    """A minimal Settings so the inner ProviderClient needs no env."""
    return Settings(dashscope_api_key="test")


@dataclass
class RouteMap:
    """A tiny URL-substring → response router for ``httpx.MockTransport``.

    Routes are matched in insertion order; the first whose substring is in the
    request URL wins. Each route counts its hits for call-count assertions.
    """

    routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]] = field(
        default_factory=list
    )
    hits: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def on(self, substring: str, response: Callable[[httpx.Request], httpx.Response]) -> RouteMap:
        self.routes.append((substring, response))
        self.hits.setdefault(substring, 0)
        return self

    def json(self, substring: str, *, status: int = 200, body: dict | None = None) -> RouteMap:
        return self.on(substring, lambda r: httpx.Response(status, json=body or {}))

    def bytes(self, substring: str, content: bytes, *, status: int = 200) -> RouteMap:
        return self.on(substring, lambda r: httpx.Response(status, content=content))

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]  # ignore query string for matching
        self.calls.append(str(request.url))
        # Most-specific match wins. A route whose substring the URL *ends with* is
        # the most specific (it pins the endpoint); among those, and otherwise,
        # the longest matching substring wins. This makes ``requests/req-1`` beat
        # the broad submit route ``fal-ai/model`` for the result endpoint, and
        # ``predictions/r1`` beat ``predictions`` for the poll, regardless of order.
        best: tuple[int, int, str, Responder] | None = None
        for substring, response in self.routes:
            if substring not in url:
                continue
            score = (1 if url.endswith(substring) else 0, len(substring))
            if best is None or score > (best[0], best[1]):
                best = (score[0], score[1], substring, response)
        if best is None:
            raise AssertionError(f"no route matched {url}")
        _, _, substring, responder = best
        self.hits[substring] += 1
        return responder(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def tripwire() -> httpx.MockTransport:
    """A transport that fails the test if any request is ever sent."""

    def _explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network must not be reached: {request.url}")

    return httpx.MockTransport(_explode)
