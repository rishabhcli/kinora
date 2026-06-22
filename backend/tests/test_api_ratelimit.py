"""Rate-limit tests — the Redis token bucket blocks past the cap (§12 security)."""

from __future__ import annotations

from httpx import AsyncClient

from app.api.deps import AUTH_RATE_CAPACITY


async def test_rate_limit_blocks_past_cap(api_client: AsyncClient) -> None:
    statuses: list[int] = []
    bodies: list[dict] = []
    for _ in range(AUTH_RATE_CAPACITY + 8):
        resp = await api_client.post(
            "/api/auth/login",
            json={"email": "ghost@example.com", "password": "whatever12"},
        )
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            bodies.append(resp.json())

    # Under the cap, requests are processed (bad creds -> 401), not throttled.
    assert statuses[0] == 401
    # Past the cap the bucket is empty and the limiter returns a typed 429.
    assert 429 in statuses
    assert bodies[0]["error"]["type"] == "rate_limited"
    assert bodies[0]["error"]["detail"]["scope"] == "auth"
