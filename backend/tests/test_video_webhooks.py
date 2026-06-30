"""Deterministic tests for the async-video webhook ingress gateway.

No infra, no network, no live model: a :class:`fastapi.testclient.TestClient` is
mounted on *just* the ``app.video.webhooks`` router (plus the gateway's typed
exception handlers) with a pre-installed test gateway whose sink is an in-memory
spy and whose signing secrets are fixtures. Every case the spec calls out is
covered: valid signed → sink once, bad signature → 401, replay/duplicate →
single processing, malformed → 4xx, unknown task tolerated, timestamp-expired →
401, oversized body → 413, plus unknown-provider 404, rate limiting, parser
normalisation, and the metrics endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_exception_handlers
from app.video.webhooks import router as webhooks_router
from app.video.webhooks.config import build_verifier
from app.video.webhooks.dedup import InMemoryDedupStore
from app.video.webhooks.gateway import WebhookGateway
from app.video.webhooks.models import CallbackStatus, ProviderCallback
from app.video.webhooks.parsers import (
    canonical_parser,
    map_status,
    minimax_parser,
    parser_for,
    wan_parser,
)
from app.video.webhooks.signing import (
    ProviderSigningConfig,
    SignatureVerifier,
    sign_body,
)

# --------------------------------------------------------------------------- #
# Test doubles + fixtures
# --------------------------------------------------------------------------- #
_WAN_SECRET = "wan-shared-secret"
_INTERNAL_SECRET = "internal-shared-secret"


class SpySink:
    """Records every callback the gateway hands it (and can simulate failure)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[ProviderCallback] = []
        self._fail = fail

    async def on_callback(self, callback: ProviderCallback) -> None:
        self.calls.append(callback)
        if self._fail:
            raise RuntimeError("simulated sink failure")


def _wan_cfg(tolerance_s: int = 300) -> ProviderSigningConfig:
    return ProviderSigningConfig(
        provider="wan",
        secret=_WAN_SECRET,
        signature_header="x-kinora-signature",
        timestamp_header="x-kinora-timestamp",
        tolerance_s=tolerance_s,
    )


def _build_app(
    gateway: WebhookGateway,
) -> tuple[FastAPI, WebhookGateway]:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(webhooks_router, prefix="/api")
    # Pre-install the test gateway so the route uses it instead of building from
    # settings — this is the same orchestrator wiring seam the route documents.
    app.state.video_webhook_gateway = gateway
    return app, gateway


@pytest.fixture
def spy() -> SpySink:
    return SpySink()


@pytest.fixture
def gateway(spy: SpySink) -> WebhookGateway:
    verifier = SignatureVerifier(clock=lambda: 1_000.0).register(_wan_cfg())
    verifier.register(
        ProviderSigningConfig(
            provider="kinora",
            secret=_INTERNAL_SECRET,
            timestamp_header=None,  # internal posts unsigned-timestamp canonical shape
        )
    )
    return WebhookGateway(
        verifier=verifier,
        sink=spy,
        dedup=InMemoryDedupStore(),
        max_body_bytes=4096,
    )


@pytest.fixture
def client(gateway: WebhookGateway) -> TestClient:
    app, _ = _build_app(gateway)
    return TestClient(app)


def _signed_headers(
    body: bytes, *, secret: str = _WAN_SECRET, ts: int = 1_000
) -> dict[str, str]:
    cfg = ProviderSigningConfig(provider="wan", secret=secret, tolerance_s=300)
    sig = sign_body(cfg, body, timestamp=str(ts))
    return {
        "x-kinora-signature": sig,
        "x-kinora-timestamp": str(ts),
        "content-type": "application/json",
    }


def _wan_body(task_id: str = "task-1", task_status: str = "SUCCEEDED", **over: object) -> bytes:
    payload: dict[str, object] = {
        "task_id": task_id,
        "task_status": task_status,
        "output": {"task_id": task_id, "task_status": task_status, "video_url": "https://x/v.mp4"},
    }
    payload.update(over)
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------- #
# Route: happy path → sink invoked exactly once, 202
# --------------------------------------------------------------------------- #
def test_valid_signed_callback_invokes_sink_once(
    client: TestClient, spy: SpySink, gateway: WebhookGateway
) -> None:
    body = _wan_body()
    resp = client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["disposition"] == "accepted"
    assert payload["task_id"] == "task-1"
    assert len(spy.calls) == 1
    cb = spy.calls[0]
    assert cb.provider == "wan"
    assert cb.status is CallbackStatus.SUCCEEDED
    assert cb.asset_url == "https://x/v.mp4"
    assert gateway.metrics.accepted == 1
    assert gateway.metrics.received == 1


# --------------------------------------------------------------------------- #
# Route: bad / missing signature → 401, sink untouched
# --------------------------------------------------------------------------- #
def test_bad_signature_rejected_401(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    headers = _signed_headers(body)
    headers["x-kinora-signature"] = "deadbeef"
    resp = client.post("/api/video/webhooks/wan", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "bad_signature"
    assert spy.calls == []


def test_missing_signature_header_rejected_401(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    resp = client.post(
        "/api/video/webhooks/wan",
        content=body,
        headers={"x-kinora-timestamp": "1000", "content-type": "application/json"},
    )
    assert resp.status_code == 401
    assert spy.calls == []


def test_signature_over_tampered_body_rejected(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    headers = _signed_headers(body)
    tampered = _wan_body(task_id="evil")
    resp = client.post("/api/video/webhooks/wan", content=tampered, headers=headers)
    assert resp.status_code == 401
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# Route: duplicate / replay delivery → processed exactly once
# --------------------------------------------------------------------------- #
def test_duplicate_delivery_processed_once(
    client: TestClient, spy: SpySink, gateway: WebhookGateway
) -> None:
    body = _wan_body()
    headers = _signed_headers(body)
    first = client.post("/api/video/webhooks/wan", content=body, headers=headers)
    second = client.post("/api/video/webhooks/wan", content=body, headers=headers)
    # Both are valid ACKs (provider stops retrying), but the sink ran once.
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["disposition"] == "accepted"
    assert second.json()["disposition"] == "duplicate"
    assert len(spy.calls) == 1
    assert gateway.metrics.accepted == 1
    assert gateway.metrics.duplicates == 1


def test_distinct_status_transitions_are_distinct_events(
    client: TestClient, spy: SpySink
) -> None:
    running = _wan_body(task_status="RUNNING")
    done = _wan_body(task_status="SUCCEEDED")
    client.post("/api/video/webhooks/wan", content=running, headers=_signed_headers(running))
    client.post("/api/video/webhooks/wan", content=done, headers=_signed_headers(done))
    # running → succeeded for one task are two events (different dedup keys).
    assert [c.status for c in spy.calls] == [CallbackStatus.RUNNING, CallbackStatus.SUCCEEDED]


# --------------------------------------------------------------------------- #
# Route: malformed payload → 422 (but signature verified first)
# --------------------------------------------------------------------------- #
def test_malformed_json_rejected_422(client: TestClient, spy: SpySink) -> None:
    body = b"not-json{{"
    resp = client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "malformed_payload"
    assert spy.calls == []


def test_payload_missing_task_id_rejected_422(client: TestClient, spy: SpySink) -> None:
    body = json.dumps({"task_status": "SUCCEEDED"}).encode()
    resp = client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 422
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# Route: unknown task id is TOLERATED (handed to sink for the reconciler)
# --------------------------------------------------------------------------- #
def test_unknown_task_id_is_tolerated(client: TestClient, spy: SpySink) -> None:
    body = _wan_body(task_id="never-seen-by-this-node")
    resp = client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 202
    assert len(spy.calls) == 1  # sink decides; the gateway does not pre-filter
    assert spy.calls[0].provider_task_id == "never-seen-by-this-node"


def test_unknown_status_is_tolerated_and_counted(
    client: TestClient, spy: SpySink, gateway: WebhookGateway
) -> None:
    body = _wan_body(task_status="weird-new-state")
    resp = client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 202
    assert spy.calls[0].status is CallbackStatus.UNKNOWN
    assert spy.calls[0].raw_status == "weird-new-state"
    assert gateway.metrics.unknown_status == 1


# --------------------------------------------------------------------------- #
# Route: timestamp outside the replay window → 401
# --------------------------------------------------------------------------- #
def test_expired_timestamp_rejected_401(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    # Verifier clock is fixed at 1000; sign with a stale ts well outside ±300s.
    headers = _signed_headers(body, ts=100)
    resp = client.post("/api/video/webhooks/wan", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "stale_callback"
    assert spy.calls == []


def test_future_timestamp_within_window_accepted(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    headers = _signed_headers(body, ts=1_200)  # +200s, inside ±300s
    resp = client.post("/api/video/webhooks/wan", content=body, headers=headers)
    assert resp.status_code == 202
    assert len(spy.calls) == 1


# --------------------------------------------------------------------------- #
# Route: oversized body → 413
# --------------------------------------------------------------------------- #
def test_oversized_body_rejected_413(client: TestClient, spy: SpySink) -> None:
    big = json.dumps({"task_id": "t", "task_status": "SUCCEEDED", "pad": "x" * 5000}).encode()
    resp = client.post("/api/video/webhooks/wan", content=big, headers=_signed_headers(big))
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "payload_too_large"
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# Route: unknown provider → 404
# --------------------------------------------------------------------------- #
def test_unknown_provider_rejected_404(client: TestClient, spy: SpySink) -> None:
    body = _wan_body()
    resp = client.post("/api/video/webhooks/nope", content=body, headers=_signed_headers(body))
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "unknown_provider"
    assert spy.calls == []


# --------------------------------------------------------------------------- #
# Route: per-source rate limiting → 429
# --------------------------------------------------------------------------- #
def test_rate_limit_returns_429() -> None:
    spy = SpySink()
    verifier = SignatureVerifier(clock=lambda: 1_000.0).register(_wan_cfg())
    gw = WebhookGateway(verifier=verifier, sink=spy, dedup=InMemoryDedupStore())
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(webhooks_router, prefix="/api")
    app.state.video_webhook_gateway = gw
    # A tiny bucket so the limit trips deterministically on the second call.
    from app.video.webhooks.ratelimit import TokenBucketRateLimiter

    app.state.video_webhook_ratelimit = TokenBucketRateLimiter(
        capacity=1, refill_per_s=0.001, clock=lambda: 0.0
    )
    tc = TestClient(app)
    body = _wan_body()
    first = tc.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    # Distinct body so it isn't a dedup; the limiter should still reject it.
    body2 = _wan_body(task_id="task-2")
    second = tc.post("/api/video/webhooks/wan", content=body2, headers=_signed_headers(body2))
    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limited"


# --------------------------------------------------------------------------- #
# Route: sink failure never 5xxs the ACK
# --------------------------------------------------------------------------- #
def test_sink_failure_is_swallowed_still_202() -> None:
    spy = SpySink(fail=True)
    verifier = SignatureVerifier(clock=lambda: 1_000.0).register(_wan_cfg())
    gw = WebhookGateway(verifier=verifier, sink=spy, dedup=InMemoryDedupStore())
    app, _ = _build_app(gw)
    tc = TestClient(app)
    body = _wan_body()
    resp = tc.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    assert resp.status_code == 202  # the provider must still see an ACK
    assert len(spy.calls) == 1


# --------------------------------------------------------------------------- #
# Route: metrics endpoint
# --------------------------------------------------------------------------- #
def test_metrics_endpoint_reports_counters(client: TestClient) -> None:
    body = _wan_body()
    client.post("/api/video/webhooks/wan", content=body, headers=_signed_headers(body))
    resp = client.get("/api/video/webhooks/_metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "wan" in data["providers"]
    assert data["metrics"]["accepted"] == 1
    assert data["metrics"]["by_provider"]["wan"] == 1


# --------------------------------------------------------------------------- #
# Internal canonical provider (no timestamp signing)
# --------------------------------------------------------------------------- #
def test_internal_canonical_callback_accepted(client: TestClient, spy: SpySink) -> None:
    body = json.dumps(
        {"task_id": "int-1", "status": "done", "asset_url": "https://x/a.mp4"}
    ).encode()
    cfg = ProviderSigningConfig(provider="kinora", secret=_INTERNAL_SECRET, timestamp_header=None)
    sig = sign_body(cfg, body)
    resp = client.post(
        "/api/video/webhooks/kinora",
        content=body,
        headers={"x-kinora-signature": sig, "content-type": "application/json"},
    )
    assert resp.status_code == 202
    assert spy.calls[0].status is CallbackStatus.SUCCEEDED


# =========================================================================== #
# Unit tests — signing
# =========================================================================== #
def _hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signing_accepts_valid_hmac_no_timestamp() -> None:
    cfg = ProviderSigningConfig(
        provider="p", secret="s", timestamp_header=None, sign_timestamp=False
    )
    v = SignatureVerifier().register(cfg)
    body = b"hello"
    v.verify("p", body, {"x-kinora-signature": _hmac("s", body)})  # no raise


def test_signing_rejects_unknown_provider() -> None:
    from app.video.webhooks.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError):
        SignatureVerifier().verify("ghost", b"x", {"x-kinora-signature": "y"})


def test_signing_timestamp_covered_by_mac() -> None:
    cfg = _wan_cfg()
    v = SignatureVerifier(clock=lambda: 1_000.0).register(cfg)
    body = b"payload"
    sig = sign_body(cfg, body, timestamp="1000")
    v.verify("wan", body, {"x-kinora-signature": sig, "x-kinora-timestamp": "1000"})
    # Tampering the timestamp alone breaks the MAC (it is signed-over).
    from app.video.webhooks.errors import SignatureError

    with pytest.raises(SignatureError):
        v.verify("wan", body, {"x-kinora-signature": sig, "x-kinora-timestamp": "1001"})


def test_signing_replay_window() -> None:
    from app.video.webhooks.errors import ReplayError

    cfg = _wan_cfg(tolerance_s=300)
    v = SignatureVerifier(clock=lambda: 10_000.0).register(cfg)
    body = b"p"
    sig = sign_body(cfg, body, timestamp="1000")  # 9000s in the past
    with pytest.raises(ReplayError):
        v.verify("wan", body, {"x-kinora-signature": sig, "x-kinora-timestamp": "1000"})


def test_signing_shared_secret_scheme() -> None:
    cfg = ProviderSigningConfig(
        provider="ss", secret="tok-123", scheme="shared_secret", timestamp_header=None
    )
    v = SignatureVerifier().register(cfg)
    v.verify("ss", b"anything", {"x-kinora-signature": "tok-123"})
    from app.video.webhooks.errors import SignatureError

    with pytest.raises(SignatureError):
        v.verify("ss", b"anything", {"x-kinora-signature": "wrong"})


def test_signing_iso_timestamp_parsed() -> None:
    cfg = ProviderSigningConfig(
        provider="iso",
        secret="s",
        timestamp_header="x-ts",
        tolerance_s=300,
    )
    # clock at the epoch matching 2026-01-01T00:00:00Z (UTC, via timegm)
    import calendar

    epoch = calendar.timegm(time.strptime("2026-01-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
    v = SignatureVerifier(clock=lambda: float(epoch)).register(cfg)
    body = b"b"
    sig = sign_body(cfg, body, timestamp="2026-01-01T00:00:00Z")
    v.verify("iso", body, {"x-kinora-signature": sig, "x-ts": "2026-01-01T00:00:00Z"})


# =========================================================================== #
# Unit tests — dedup
# =========================================================================== #
@pytest.mark.asyncio
async def test_dedup_first_claim_wins() -> None:
    store = InMemoryDedupStore()
    assert await store.claim("k") is True
    assert await store.claim("k") is False
    assert await store.claim("other") is True


@pytest.mark.asyncio
async def test_dedup_ttl_expiry() -> None:
    now = {"t": 0.0}
    store = InMemoryDedupStore(ttl_s=10.0, clock=lambda: now["t"])
    assert await store.claim("k") is True
    now["t"] = 5.0
    assert await store.claim("k") is False  # still within ttl
    now["t"] = 20.0
    assert await store.claim("k") is True  # expired, re-claimable
    assert store.seen_count() == 1


@pytest.mark.asyncio
async def test_dedup_capacity_eviction() -> None:
    store = InMemoryDedupStore(max_entries=2)
    await store.claim("a")
    await store.claim("b")
    await store.claim("c")  # evicts LRU "a"
    assert store.seen_count() == 2


# =========================================================================== #
# Unit tests — parsers
# =========================================================================== #
def test_map_status_tolerant() -> None:
    assert map_status("SUCCEEDED") is CallbackStatus.SUCCEEDED
    assert map_status("done") is CallbackStatus.SUCCEEDED
    assert map_status("error") is CallbackStatus.FAILED
    assert map_status("canceled") is CallbackStatus.CANCELLED
    assert map_status("processing") is CallbackStatus.RUNNING
    assert map_status("brand-new-state") is CallbackStatus.UNKNOWN
    assert map_status(None) is CallbackStatus.UNKNOWN


def test_wan_parser_extracts_results_url() -> None:
    payload = {
        "task_id": "t1",
        "task_status": "SUCCEEDED",
        "output": {"task_id": "t1", "task_status": "SUCCEEDED", "results": [{"url": "u://v"}]},
    }
    cb = wan_parser("wan", payload)
    assert cb.asset_url == "u://v"
    assert cb.status is CallbackStatus.SUCCEEDED
    assert cb.asset_kind == "video"


def test_wan_parser_failed_carries_error() -> None:
    payload = {"task_id": "t1", "task_status": "FAILED", "code": "X", "message": "boom"}
    cb = wan_parser("wan", payload)
    assert cb.status is CallbackStatus.FAILED
    assert cb.error_code == "X" and cb.error_message == "boom"


def test_minimax_parser() -> None:
    payload = {"task_id": "m1", "status": "Success", "download_url": "d://v", "file_id": "f9"}
    cb = minimax_parser("minimax", payload)
    assert cb.status is CallbackStatus.SUCCEEDED
    assert cb.asset_url == "d://v"
    assert cb.metadata["file_id"] == "f9"


def test_canonical_parser_explicit_event_id_is_dedup_key() -> None:
    cb = canonical_parser(
        "kinora", {"task_id": "t", "status": "done", "event_id": "evt-42"}
    )
    assert cb.idempotency_key == "evt-42"
    assert cb.dedup_key == "kinora:evt-42"


def test_canonical_parser_derived_dedup_key_includes_status() -> None:
    cb = canonical_parser("kinora", {"task_id": "t", "status": "running"})
    assert cb.idempotency_key == "t:running"


def test_parser_for_falls_back_to_canonical() -> None:
    assert parser_for("brand-new-provider") is canonical_parser
    assert parser_for("wan") is wan_parser


# =========================================================================== #
# Unit tests — config builder
# =========================================================================== #
class _StubSettings:
    video_webhook_wan_secret = "wan-secret"
    video_webhook_dashscope_secret = None
    video_webhook_minimax_secret = "mm-secret"
    video_webhook_internal_secret = None
    video_webhook_tolerance_s = 120


def test_build_verifier_registers_only_configured_providers() -> None:
    v = build_verifier(_StubSettings())
    assert set(v.providers()) == {"wan", "minimax"}
    assert v.config_for("wan").tolerance_s == 120


def test_build_verifier_empty_settings_registers_nothing() -> None:
    v = build_verifier(object())
    assert v.providers() == []


# =========================================================================== #
# Model — terminal-status helper
# =========================================================================== #
def test_callback_status_is_terminal() -> None:
    assert CallbackStatus.SUCCEEDED.is_terminal
    assert CallbackStatus.FAILED.is_terminal
    assert CallbackStatus.CANCELLED.is_terminal
    assert not CallbackStatus.RUNNING.is_terminal
    assert not CallbackStatus.UNKNOWN.is_terminal
