"""Problem+json envelope + domain-error mapping (no infra; TestClient only)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.apihardening.config import HardeningConfig
from app.apihardening.problem import (
    PROBLEM_MAP,
    PROBLEM_MEDIA_TYPE,
    Problem,
    ProblemException,
    install_problem_handlers,
    map_exception,
)
from app.memory.budget_service import BudgetExceeded
from app.providers.errors import LiveVideoDisabled, ProviderError


def _problem_app(*, expose: bool = True) -> FastAPI:
    cfg = HardeningConfig(problem_json_enabled=True, expose_internal_errors=expose)
    app = FastAPI()
    install_problem_handlers(app, config=cfg)

    class Body(BaseModel):
        n: int

    @app.get("/raise-problem")
    async def _problem() -> None:
        raise ProblemException(
            "teapot", "I am a teapot", status=418, detail="short and stout", extensions={"k": "v"}
        )

    @app.get("/budget")
    async def _budget() -> None:
        raise BudgetExceeded("ceiling", requested=10.0, used=95.0, cap=100.0)

    @app.get("/live")
    async def _live() -> None:
        raise LiveVideoDisabled("gated off")

    @app.get("/provider")
    async def _provider() -> None:
        raise ProviderError("upstream blew up", code="InternalError", status_code=500)

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("a secret stack-trace detail")

    @app.post("/validate")
    async def _validate(body: Body) -> dict[str, int]:
        return {"n": body.n}

    return app


def test_problem_model_renders_rfc7807() -> None:
    problem = Problem(title="Nope", status=400, code="nope", detail="because")
    resp = problem.to_response()
    assert resp.media_type == PROBLEM_MEDIA_TYPE
    assert resp.status_code == 400


def test_problem_exception_maps_with_extensions() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.get("/raise-problem")
    assert resp.status_code == 418
    assert resp.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = resp.json()
    assert body["code"] == "teapot"
    assert body["status"] == 418
    assert body["title"] == "I am a teapot"
    assert body["detail"] == "short and stout"
    # Extension member rides along flat (RFC-7807 allows arbitrary members).
    assert body["k"] == "v"
    assert body["type"].endswith("/teapot")


def test_budget_exceeded_maps_to_402_problem() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.get("/budget")
    assert resp.status_code == 402
    body = resp.json()
    assert body["code"] == "budget_exceeded"
    assert body["cap"] == 100.0
    assert body["scope"] == "ceiling"


def test_live_video_disabled_maps_to_409_problem() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.get("/live")
    assert resp.status_code == 409
    assert resp.json()["code"] == "live_video_disabled"


def test_provider_error_maps_to_502_problem() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.get("/provider")
    assert resp.status_code == 502
    body = resp.json()
    assert body["code"] == "provider_error"
    assert body["provider_code"] == "InternalError"


def test_unexpected_error_scrubbed_outside_local() -> None:
    client = TestClient(_problem_app(expose=False), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    # The secret detail must never leak when expose_internal_errors is off.
    assert "secret" not in body.get("detail", "")
    assert body["detail"] == "internal server error"


def test_unexpected_error_detail_shown_in_local() -> None:
    client = TestClient(_problem_app(expose=True), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert "secret" in resp.json()["detail"]


def test_validation_error_maps_to_422_problem() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.post("/validate", json={"n": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["errors"], list)
    assert body["errors"]


def test_not_found_maps_to_404_problem() -> None:
    client = TestClient(_problem_app(), raise_server_exceptions=False)
    resp = client.get("/missing")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_map_exception_handles_api_error_by_name() -> None:
    # The gateway's APIError carries its own status/type/detail; map_exception
    # must honour it without importing the class.
    from app.api.errors import APIError

    cfg = HardeningConfig(problem_json_enabled=True)
    resp = map_exception(APIError("custom", "boom", status=403, detail={"a": 1}), config=cfg)
    assert resp.status_code == 403


def test_problem_map_is_stable_contract() -> None:
    # These stable codes are a wire contract — guard against accidental drift.
    assert PROBLEM_MAP["BudgetExceeded"][0] == "budget_exceeded"
    assert PROBLEM_MAP["ProviderError"][1] == 502
