"""Request-id / correlation-id middleware (mint/echo + response stamping)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.apihardening.config import HardeningConfig
from app.apihardening.requestid import RequestIdMiddleware, current_request_id


def _rid_app(config: HardeningConfig) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware, config=config)

    @app.get("/who")
    async def _who() -> dict[str, str | None]:
        return {"request_id": current_request_id()}

    return app


def test_mints_request_id_when_absent() -> None:
    client = TestClient(_rid_app(HardeningConfig()))
    resp = client.get("/who")
    rid = resp.headers["x-request-id"]
    assert rid
    # The contextvar value seen inside the handler matches the response header.
    assert resp.json()["request_id"] == rid


def test_echoes_trusted_inbound_id() -> None:
    client = TestClient(_rid_app(HardeningConfig(trust_inbound_request_id=True)))
    resp = client.get("/who", headers={"X-Request-ID": "client-supplied-123"})
    assert resp.headers["x-request-id"] == "client-supplied-123"


def test_untrusted_inbound_id_is_replaced() -> None:
    client = TestClient(_rid_app(HardeningConfig(trust_inbound_request_id=False)))
    resp = client.get("/who", headers={"X-Request-ID": "client-supplied-123"})
    assert resp.headers["x-request-id"] != "client-supplied-123"


def test_unsafe_inbound_id_is_rejected_and_minted() -> None:
    # A header with control chars / too-long is not echoed (injection defence).
    client = TestClient(_rid_app(HardeningConfig(trust_inbound_request_id=True)))
    resp = client.get("/who", headers={"X-Request-ID": "x" * 500})
    assert resp.headers["x-request-id"] != "x" * 500


def test_correlation_id_defaults_to_request_id() -> None:
    client = TestClient(_rid_app(HardeningConfig()))
    resp = client.get("/who")
    assert resp.headers["x-correlation-id"] == resp.headers["x-request-id"]


def test_correlation_id_echoed_independently() -> None:
    client = TestClient(_rid_app(HardeningConfig()))
    resp = client.get("/who", headers={"X-Correlation-ID": "trace-abc"})
    assert resp.headers["x-correlation-id"] == "trace-abc"


def test_request_id_cleared_outside_request() -> None:
    client = TestClient(_rid_app(HardeningConfig()))
    client.get("/who")
    # The contextvar must reset after the request completes.
    assert current_request_id() is None
