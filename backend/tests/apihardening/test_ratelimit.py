"""Token-bucket rate limiter middleware (in-memory store; deterministic clock)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.apihardening.config import HardeningConfig
from app.apihardening.ratelimit import (
    BucketResult,
    InMemoryTokenBucketStore,
    RateLimitMiddleware,
    RateLimitRule,
    resolve_redis,
)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _rl_app(
    store: InMemoryTokenBucketStore,
    *,
    config: HardeningConfig,
    rules: tuple[RateLimitRule, ...] = (),
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, store=store, config=config, rules=rules)

    @app.get("/ping")
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/regen")
    async def _regen() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_allows_within_capacity_then_429() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(rate_limit_capacity=3, rate_limit_refill_per_s=0.0)
    client = TestClient(_rl_app(store, config=cfg))
    # 3 allowed, 4th rejected (no refill at the same clock instant).
    for _ in range(3):
        assert client.get("/ping").status_code == 200
    blocked = client.get("/ping")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]
    assert blocked.json()["error"]["type"] == "rate_limited"


def test_ratelimit_headers_present() -> None:
    store = InMemoryTokenBucketStore()
    cfg = HardeningConfig(rate_limit_capacity=10, rate_limit_refill_per_s=1.0)
    client = TestClient(_rl_app(store, config=cfg))
    resp = client.get("/ping")
    assert resp.headers["ratelimit-limit"] == "10"
    assert int(resp.headers["ratelimit-remaining"]) == 9
    assert "ratelimit-reset" in resp.headers


def test_refill_restores_tokens() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(rate_limit_capacity=1, rate_limit_refill_per_s=1.0)
    client = TestClient(_rl_app(store, config=cfg))
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 429
    clock.t = 1.5  # >= 1 token refilled
    assert client.get("/ping").status_code == 200


def test_exempt_paths_never_limited() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(rate_limit_capacity=1, rate_limit_refill_per_s=0.0)
    client = TestClient(_rl_app(store, config=cfg))
    # /health is in the default exempt prefixes — unlimited.
    for _ in range(20):
        assert client.get("/health").status_code == 200


def test_per_route_rule_overrides_default() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(rate_limit_capacity=100, rate_limit_refill_per_s=0.0)
    rule = RateLimitRule(scope="regen", path_prefix="/regen", capacity=2, refill_per_s=0.0)
    client = TestClient(_rl_app(store, config=cfg, rules=(rule,)))
    assert client.get("/regen").status_code == 200
    assert client.get("/regen").status_code == 200
    assert client.get("/regen").status_code == 429
    # The default route is independent and still has its big budget.
    assert client.get("/ping").status_code == 200


def test_disabled_limiter_passes_through() -> None:
    store = InMemoryTokenBucketStore()
    cfg = HardeningConfig(rate_limit_enabled=False, rate_limit_capacity=1)
    client = TestClient(_rl_app(store, config=cfg))
    for _ in range(10):
        assert client.get("/ping").status_code == 200


def test_problem_json_mode_429_envelope() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(
        rate_limit_capacity=1, rate_limit_refill_per_s=0.0, problem_json_enabled=True
    )
    client = TestClient(_rl_app(store, config=cfg))
    client.get("/ping")
    blocked = client.get("/ping")
    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("application/problem+json")
    assert blocked.json()["code"] == "rate_limited"


def test_bucket_result_retry_after_and_reset() -> None:
    result = BucketResult(allowed=False, remaining=0.0, capacity=10, refill_per_s=2.0)
    assert result.retry_after_seconds() == 1
    assert result.reset_seconds == 5
    full = BucketResult(allowed=True, remaining=10.0, capacity=10, refill_per_s=2.0)
    assert full.reset_seconds == 0


async def test_inmemory_store_consume_math() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    r1 = await store.consume("k", capacity=2, refill_per_s=1.0)
    assert r1.allowed and r1.remaining == 1.0
    r2 = await store.consume("k", capacity=2, refill_per_s=1.0)
    assert r2.allowed and r2.remaining == 0.0
    r3 = await store.consume("k", capacity=2, refill_per_s=1.0)
    assert not r3.allowed
    clock.t = 1.0  # one token refilled
    r4 = await store.consume("k", capacity=2, refill_per_s=1.0)
    assert r4.allowed


def test_resolve_redis_handles_callable_and_none() -> None:
    assert resolve_redis(None) is None
    sentinel = object()
    assert resolve_redis(lambda: sentinel) is sentinel
    assert resolve_redis(sentinel) is sentinel

    def _raises() -> object:
        raise RuntimeError("no container")

    assert resolve_redis(_raises) is None


def test_distinct_identities_have_separate_buckets() -> None:
    clock = _FakeClock()
    store = InMemoryTokenBucketStore(clock=clock)
    cfg = HardeningConfig(rate_limit_capacity=1, rate_limit_refill_per_s=0.0)
    app = _rl_app(store, config=cfg)
    # Two different bearer subjects (unverified, identity-only) get own buckets.
    import base64
    import json

    def _token(sub: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
        return f"x.{payload}.y"

    client = TestClient(app)
    a = {"Authorization": f"Bearer {_token('user-a')}"}
    b = {"Authorization": f"Bearer {_token('user-b')}"}
    assert client.get("/ping", headers=a).status_code == 200
    assert client.get("/ping", headers=a).status_code == 429
    # user-b's bucket is untouched.
    assert client.get("/ping", headers=b).status_code == 200
