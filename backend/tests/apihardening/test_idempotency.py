"""Idempotency-Key store-and-replay (in-memory store; TestClient only)."""

from __future__ import annotations

import itertools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.apihardening.config import HardeningConfig
from app.apihardening.idempotency import (
    IdempotencyMiddleware,
    IdempotencyRecord,
    InMemoryIdempotencyStore,
)


def _idem_app(store: InMemoryIdempotencyStore, *, config: HardeningConfig | None = None) -> FastAPI:
    from app.api.errors import install_exception_handlers

    app = FastAPI()
    # Install the gateway's typed handlers so a route's APIError becomes its mapped
    # status (the live app always has these), exercising the realistic case where
    # the idempotency middleware sees a non-2xx response — not a raised exception.
    install_exception_handlers(app)
    cfg = config or HardeningConfig()
    app.add_middleware(IdempotencyMiddleware, store=store, config=cfg)
    counter = itertools.count(1)

    @app.post("/create")
    async def _create() -> dict[str, int]:
        # A side-effectful op: each *real* execution gets a fresh id.
        return {"id": next(counter)}

    @app.post("/fail")
    async def _fail() -> dict[str, str]:
        from app.api.errors import APIError

        raise APIError("nope", "deliberate failure", status=400)

    @app.get("/get")
    async def _get() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_first_request_runs_and_marks_not_replayed() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    resp = client.post("/create", headers={"Idempotency-Key": "abc"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 1}
    assert resp.headers["idempotency-replayed"] == "false"


def test_replay_returns_identical_response() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    first = client.post("/create", headers={"Idempotency-Key": "abc"})
    second = client.post("/create", headers={"Idempotency-Key": "abc"})
    # The route was NOT re-executed: same id, replay flag flips to true.
    assert second.status_code == 200
    assert second.json() == first.json() == {"id": 1}
    assert second.headers["idempotency-replayed"] == "true"


def test_different_keys_execute_independently() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    a = client.post("/create", headers={"Idempotency-Key": "a"})
    b = client.post("/create", headers={"Idempotency-Key": "b"})
    assert a.json() == {"id": 1}
    assert b.json() == {"id": 2}


def test_no_key_means_no_dedup() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    a = client.post("/create")
    b = client.post("/create")
    assert a.json() == {"id": 1}
    assert b.json() == {"id": 2}


def test_key_reuse_with_different_body_is_rejected() -> None:
    store = InMemoryIdempotencyStore()
    cfg = HardeningConfig(allowed_content_types=frozenset())
    client = TestClient(_idem_app(store, config=cfg))
    client.post("/create", headers={"Idempotency-Key": "k"}, json={"x": 1})
    resp = client.post("/create", headers={"Idempotency-Key": "k"}, json={"x": 2})
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "idempotency_key_reuse"


def test_failed_response_is_not_cached() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    first = client.post("/fail", headers={"Idempotency-Key": "f"})
    assert first.status_code == 400
    # A non-2xx is not stored, so the key is reusable: the second call re-runs the
    # route (replayed=false again) rather than replaying a cached failure.
    second = client.post("/fail", headers={"Idempotency-Key": "f"})
    assert second.status_code == 400
    assert second.headers["idempotency-replayed"] == "false"


def test_empty_key_is_rejected() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    resp = client.post("/create", headers={"Idempotency-Key": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_idempotency_key"


def test_overlong_key_is_rejected() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    resp = client.post("/create", headers={"Idempotency-Key": "x" * 5000})
    assert resp.status_code == 400


def test_non_post_methods_bypass_idempotency() -> None:
    store = InMemoryIdempotencyStore()
    client = TestClient(_idem_app(store))
    resp = client.get("/get", headers={"Idempotency-Key": "g"})
    assert resp.status_code == 200
    assert "idempotency-replayed" not in {k.lower() for k in resp.headers}


def test_problem_json_mode_replays_problem_envelope() -> None:
    store = InMemoryIdempotencyStore()
    cfg = HardeningConfig(problem_json_enabled=True, allowed_content_types=frozenset())
    client = TestClient(_idem_app(store, config=cfg))
    client.post("/create", headers={"Idempotency-Key": "k"}, json={"x": 1})
    resp = client.post("/create", headers={"Idempotency-Key": "k"}, json={"x": 2})
    assert resp.status_code == 422
    assert resp.json()["code"] == "idempotency_key_reuse"


async def test_in_progress_rejected_409() -> None:
    # Drive the store directly: a pending reservation -> the next begin is 409.
    store = InMemoryIdempotencyStore()
    assert await store.begin("k", "fp", ttl_s=60) == "new"
    assert await store.begin("k", "fp", ttl_s=60) == "in_progress"


async def test_store_ttl_expiry_allows_fresh_run() -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    store = InMemoryIdempotencyStore(clock=lambda: next(ticks))
    assert await store.begin("k", "fp", ttl_s=10) == "new"  # clock=0
    await store.complete("k", IdempotencyRecord("fp", 200, [], b"{}"), ttl_s=10)  # clock=0
    # clock now 100 > 0+10 -> the record has expired, begin is fresh again.
    assert await store.begin("k", "fp", ttl_s=10) == "new"


def test_record_json_roundtrip() -> None:
    record = IdempotencyRecord("fp", 201, [("content-type", "application/json")], b'{"a":1}')
    restored = IdempotencyRecord.from_json(record.to_json())
    assert restored == record


@pytest.mark.parametrize("status", [200, 201, 204])
def test_2xx_statuses_are_cached(status: int) -> None:
    store = InMemoryIdempotencyStore()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, store=store, config=HardeningConfig())

    @app.post("/s")
    async def _s() -> object:
        from fastapi import Response

        return Response(status_code=status)

    client = TestClient(app)
    client.post("/s", headers={"Idempotency-Key": "k"})
    replay = client.post("/s", headers={"Idempotency-Key": "k"})
    assert replay.headers["idempotency-replayed"] == "true"
