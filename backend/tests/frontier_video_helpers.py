"""Shared, deterministic test helpers for the frontier video adapters.

No network, no real keys: a recorded-style :class:`httpx.MockTransport` plays back
fixture responses keyed by (method, path), records every issued request, and an
injected no-op sleeper makes poll/backoff loops instant.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.config import Settings
from app.video.adapters.frontier.transport import FrontierRetryConfig, FrontierTransport

#: A handler maps an httpx.Request → httpx.Response (the MockTransport contract).
Handler = Callable[[httpx.Request], httpx.Response]

#: Zeroed retry/backoff so transport retries run instantly and deterministically.
FAST_RETRY = FrontierRetryConfig(
    max_attempts=4,
    backoff_base_s=0.0,
    backoff_max_s=0.0,
    backoff_jitter_s=0.0,
)


async def no_sleep(_seconds: float) -> None:
    """An injected sleeper that never actually waits."""
    return None


def frontier_settings(*, live: bool = True, enabled: bool = True, **overrides: object) -> Settings:
    """Settings with the frontier flag + live gate set as requested."""
    base: dict[str, object] = {
        "dashscope_api_key": "test",
        "kinora_live_video": live,
        "frontier_video_enabled": enabled,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_transport(
    handler: Handler,
    *,
    provider: str,
    base_url: str = "https://example.test/v1",
    api_key: str | None = "sk-test",
    enabled: bool = True,
    error_mapper: object | None = None,
    auth_scheme: str = "Bearer",
    extra_headers: dict[str, str] | None = None,
) -> FrontierTransport:
    """A FrontierTransport wired to a MockTransport handler + no-op sleeper."""
    return FrontierTransport(
        base_url=base_url,
        api_key=api_key,
        provider=provider,
        enabled=enabled,
        transport=httpx.MockTransport(handler),
        retry=FAST_RETRY,
        error_mapper=error_mapper,  # type: ignore[arg-type]
        sleeper=no_sleep,
        auth_scheme=auth_scheme,
        extra_headers=extra_headers,
    )


class RecordingHandler:
    """A scriptable MockTransport handler that records requests and counts hits.

    ``routes`` maps a path *suffix* → a function(request) → httpx.Response (or a
    static httpx.Response). Unmatched paths return 500 so a missing route is loud.
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes
        self.requests: list[httpx.Request] = []
        self.hits: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for suffix, responder in self._routes.items():
            if path.endswith(suffix):
                self.hits[suffix] = self.hits.get(suffix, 0) + 1
                if callable(responder):
                    result = responder(request)
                    assert isinstance(result, httpx.Response)
                    return result
                assert isinstance(responder, httpx.Response)
                return responder
        return httpx.Response(500, json={"error": f"unrouted path {path}"})
