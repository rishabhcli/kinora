"""Tests for the /api/optim cost + perf endpoints (infra-free: auth dep overridden)."""

from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes.optim import build_cost_report, build_perf_report, router
from app.optim.cost_meter import (
    CostMeter,
    Price,
    cost_context,
    reset_cost_meter,
    set_cost_meter,
)
from app.providers.types import Usage


def test_build_cost_report_has_priced_models_and_rollup() -> None:
    meter = CostMeter(pricing={"a": Price(input_per_1k=Decimal("0.001"))})
    meter(Usage(model="a", operation="chat", input_tokens=2000))
    report = build_cost_report(meter)
    assert report["priced_models"] == ["a"]
    assert Decimal(report["rollup"]["total"]["cost_usd"]) == Decimal("0.002")


def test_build_perf_report_is_compact() -> None:
    meter = CostMeter(pricing={"a": Price(input_per_1k=Decimal("0.001"))})
    meter(Usage(model="a", operation="vl", input_tokens=1000))
    report = build_perf_report(meter, uptime_s=12.34)
    assert report["uptime_s"] == 12.3
    assert report["priced_model_count"] == 1
    assert "vl" in report["by_operation"]
    assert "by_model" not in report  # compact: no full per-model breakdown


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: object()  # bypass auth
    return app


def test_cost_endpoint_reflects_the_process_meter() -> None:
    reset_cost_meter()
    meter = CostMeter(pricing={"qwen3.7-max": Price(input_per_1k=Decimal("0.01"))})
    with cost_context(book_id="bk9"):
        meter(Usage(model="qwen3.7-max", operation="chat", input_tokens=1000))
    set_cost_meter(meter)
    try:
        resp = TestClient(_app()).get("/api/optim/cost")
        assert resp.status_code == 200
        body = resp.json()
        assert body["priced_models"] == ["qwen3.7-max"]
        assert Decimal(body["rollup"]["by_book"]["bk9"]["cost_usd"]) == Decimal("0.01")
    finally:
        reset_cost_meter()


def test_perf_endpoint_returns_200_and_uptime() -> None:
    reset_cost_meter()
    try:
        resp = TestClient(_app()).get("/api/optim/perf")
        assert resp.status_code == 200
        assert "uptime_s" in resp.json()
    finally:
        reset_cost_meter()
