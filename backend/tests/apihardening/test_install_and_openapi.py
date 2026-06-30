"""End-to-end wiring via install() + OpenAPI customization (minimal app)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.apihardening import (
    HardeningConfig,
    InMemoryIdempotencyStore,
    InMemoryTokenBucketStore,
    install,
)
from app.apihardening.openapi import install_openapi


def _wired_app(**kwargs: object) -> tuple[FastAPI, HardeningConfig]:
    app = FastAPI(title="Test", version="9.9.9")
    cfg = HardeningConfig(
        rate_limit_capacity=2,
        rate_limit_refill_per_s=0.0,
        **dict(kwargs),  # type: ignore[arg-type]
    )
    effective = install(
        app,
        config=cfg,
        rate_limit_store=InMemoryTokenBucketStore(),
        idempotency_store=InMemoryIdempotencyStore(),
    )

    counter = {"n": 0}

    @app.post("/thing")
    async def _thing() -> dict[str, int]:
        counter["n"] += 1
        return {"n": counter["n"]}

    @app.get("/thing")
    async def _get_thing() -> dict[str, str]:
        return {"ok": "yes"}

    return app, effective


def test_install_stacks_all_middleware() -> None:
    app, _ = _wired_app()
    names = {getattr(m.cls, "__name__", "") for m in app.user_middleware}
    assert {
        "RequestIdMiddleware",
        "RequestLimitsMiddleware",
        "RateLimitMiddleware",
        "IdempotencyMiddleware",
    } <= names


def test_full_stack_request_id_and_idempotency() -> None:
    app, _ = _wired_app()
    client = TestClient(app)
    hdr = {"Idempotency-Key": "k", "content-type": "application/json"}
    first = client.post("/thing", headers=hdr, content=b"{}")
    assert first.status_code == 200
    assert first.headers["x-request-id"]
    assert first.json() == {"n": 1}
    # Replay: same body, route not re-run, fresh request-id on the replay.
    second = client.post("/thing", headers=hdr, content=b"{}")
    assert second.json() == {"n": 1}
    assert second.headers["idempotency-replayed"] == "true"
    assert second.headers["x-request-id"] != first.headers["x-request-id"]


def test_full_stack_rate_limit_then_429() -> None:
    app, _ = _wired_app()
    client = TestClient(app)
    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 200
    blocked = client.get("/thing")
    assert blocked.status_code == 429
    # The request-id middleware still stamped the 429.
    assert blocked.headers["x-request-id"]


def test_legacy_envelope_preserved_by_default() -> None:
    # problem_json off -> a rate-limit rejection uses the legacy {error:{...}} shape.
    app, cfg = _wired_app()
    assert cfg.problem_json_enabled is False
    client = TestClient(app)
    client.get("/thing")
    client.get("/thing")
    blocked = client.get("/thing")
    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("application/json")
    assert "error" in blocked.json()
    assert blocked.json()["error"]["type"] == "rate_limited"


def test_openapi_documents_problem_and_security() -> None:
    app = FastAPI(title="Doc", version="1.0.0")

    @app.post("/op")
    async def _op() -> dict[str, str]:
        return {"ok": "yes"}

    install_openapi(app, config=HardeningConfig(problem_json_enabled=True))
    schema = app.openapi()
    comps = schema["components"]
    assert "Problem" in comps["schemas"]
    assert "BearerAuth" in comps["securitySchemes"]
    assert "ApiKeyAuth" in comps["securitySchemes"]
    op = schema["paths"]["/op"]["post"]
    # Standard error responses are attached.
    for status in ("400", "401", "429", "500"):
        assert status in op["responses"]
    # The Idempotency-Key request header is documented on POST.
    params = op.get("parameters", [])
    assert any(p["name"] == "Idempotency-Key" for p in params)


def test_openapi_legacy_mode_points_at_error_response() -> None:
    app = FastAPI(title="Doc", version="1.0.0")

    @app.get("/op")
    async def _op() -> dict[str, str]:
        return {"ok": "yes"}

    install_openapi(app, config=HardeningConfig(problem_json_enabled=False))
    schema = app.openapi()
    resp_404 = schema["paths"]["/op"]["get"]["responses"]["404"]
    ref = resp_404["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ErrorResponse")


def test_install_can_disable_individual_concerns() -> None:
    app = FastAPI()
    install(
        app,
        config=HardeningConfig(),
        enable_idempotency=False,
        enable_rate_limit=False,
        enable_request_limits=False,
        enable_request_id=True,
        enable_openapi=False,
    )
    names = {getattr(m.cls, "__name__", "") for m in app.user_middleware}
    assert "RequestIdMiddleware" in names
    assert "IdempotencyMiddleware" not in names
    assert "RateLimitMiddleware" not in names
