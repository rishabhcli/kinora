"""Unit tests for the OAuth2 client, webhook verifier, and health projection."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.db.models.integration import AppConnection, ConnectionStatus, SyncRun, SyncRunStatus
from app.integrations.clock import FakeClock
from app.integrations.errors import AuthExpired, ConfigurationError, WebhookVerificationError
from app.integrations.health import ConnectionHealth, SyncRunView
from app.integrations.http import FakeHttpClient
from app.integrations.oauth import OAuth2Client, OAuth2Config, TokenSet
from app.integrations.webhooks import WebhookSecret, WebhookVerifier


def _config(**over: object) -> OAuth2Config:
    base: dict[str, object] = {
        "provider": "notion",
        "client_id": "cid",
        "client_secret": "csecret",
        "authorize_endpoint": "https://prov/oauth/authorize",
        "token_endpoint": "https://prov/oauth/token",
        "redirect_uri": "https://kinora/cb",
        "scopes": ("read",),
    }
    base.update(over)
    return OAuth2Config(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# OAuth2 client
# --------------------------------------------------------------------------- #
def test_authorize_url_has_required_params() -> None:
    client = OAuth2Client(_config(), FakeHttpClient(), clock=FakeClock())
    url, state = client.authorize_url(state="fixed-state")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://kinora/cb"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["fixed-state"]
    assert q["scope"] == ["read"]
    assert state == "fixed-state"


def test_incomplete_config_raises() -> None:
    client = OAuth2Client(_config(client_id=""), FakeHttpClient())
    with pytest.raises(ConfigurationError):
        client.authorize_url()


@pytest.mark.asyncio
async def test_exchange_code_parses_token_and_expiry() -> None:
    payload = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 3600,
        "scope": "read write",
        "workspace_name": "My Space",
    }
    http = FakeHttpClient().json_response("POST", "/oauth/token", payload)
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    client = OAuth2Client(_config(), http, clock=clock)
    token = await client.exchange_code("the-code")
    assert token.access_token == "AT" and token.refresh_token == "RT"
    assert token.expires_at == datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    assert token.extra["workspace_name"] == "My Space"
    # form posted
    form = http.requests[0].data
    assert isinstance(form, dict) and form["grant_type"] == "authorization_code"


@pytest.mark.asyncio
async def test_refresh_keeps_old_refresh_token_when_absent() -> None:
    http = FakeHttpClient().json_response(
        "POST", "/oauth/token", {"access_token": "AT2", "expires_in": 60}
    )
    client = OAuth2Client(_config(), http, clock=FakeClock())
    token = await client.refresh("OLD-RT")
    assert token.access_token == "AT2"
    assert token.refresh_token == "OLD-RT"  # preserved


@pytest.mark.asyncio
async def test_token_endpoint_without_access_token_raises() -> None:
    http = FakeHttpClient().json_response("POST", "/oauth/token", {"error": "bad"})
    client = OAuth2Client(_config(), http)
    with pytest.raises(AuthExpired):
        await client.exchange_code("x")


def test_tokenset_blob_roundtrip_and_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    token = TokenSet(
        access_token="A", refresh_token="R", scope="s",
        expires_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC), extra={"k": "v"},
    )
    blob = token.as_blob()
    restored = TokenSet.from_blob(blob)
    assert restored.access_token == "A" and restored.extra == {"k": "v"}
    # expired with skew
    assert token.is_expired(now=datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    assert not token.is_expired(now=now)
    assert not TokenSet(access_token="A").is_expired(now=now)  # no expiry => never


# --------------------------------------------------------------------------- #
# Webhook verifier
# --------------------------------------------------------------------------- #
def _sig(secret: str, body: bytes, prefix: str = "") -> str:
    return prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_verify_accepts_valid_signature() -> None:
    body = json.dumps({"event": "new"}).encode()
    verifier = WebhookVerifier().register(
        WebhookSecret(provider="readwise", secret="shh", signature_header="x-signature")
    )
    headers = {"X-Signature": _sig("shh", body)}
    verifier.verify("readwise", body, headers)  # no raise


def test_webhook_verify_rejects_bad_signature() -> None:
    verifier = WebhookVerifier().register(WebhookSecret(provider="readwise", secret="shh"))
    with pytest.raises(WebhookVerificationError):
        verifier.verify("readwise", b"body", {"x-signature": "deadbeef"})


def test_webhook_verify_rejects_missing_header() -> None:
    verifier = WebhookVerifier().register(WebhookSecret(provider="readwise", secret="shh"))
    with pytest.raises(WebhookVerificationError):
        verifier.verify("readwise", b"body", {})


def test_webhook_verify_unknown_provider_raises() -> None:
    with pytest.raises(WebhookVerificationError):
        WebhookVerifier().verify("nope", b"x", {"x-signature": "y"})


def test_webhook_prefix_signature() -> None:
    body = b"payload"
    verifier = WebhookVerifier().register(
        WebhookSecret(
            provider="gh", secret="k", signature_header="x-hub-signature-256",
            signature_prefix="sha256=",
        )
    )
    headers = {"X-Hub-Signature-256": _sig("k", body, prefix="sha256=")}
    verifier.verify("gh", body, headers)


# --------------------------------------------------------------------------- #
# Health projection
# --------------------------------------------------------------------------- #
def _conn(**over: object) -> AppConnection:
    conn = AppConnection(
        id="c1", user_id="u1", provider="readwise", status=ConnectionStatus.ACTIVE,
        config={}, consecutive_failures=0,
    )
    for k, v in over.items():
        setattr(conn, k, v)
    return conn


def test_health_healthy() -> None:
    h = ConnectionHealth.of(_conn(), imported_count=5)
    assert h.health == "healthy" and h.imported_count == 5 and not h.needs_reauth


def test_health_degraded_on_failures() -> None:
    h = ConnectionHealth.of(_conn(consecutive_failures=1))
    assert h.health == "degraded"


def test_health_needs_attention_on_reauth() -> None:
    h = ConnectionHealth.of(_conn(status=ConnectionStatus.NEEDS_REAUTH))
    assert h.health == "needs_attention" and h.needs_reauth


def test_health_down_on_error() -> None:
    assert ConnectionHealth.of(_conn(status=ConnectionStatus.ERROR)).health == "down"


def test_sync_run_view_projection() -> None:
    run = SyncRun(
        id="r1", connection_id="c1", status=SyncRunStatus.PARTIAL, trigger="manual",
        items_seen=3, items_imported=2, items_skipped=0, items_failed=1, error=None,
    )
    view = SyncRunView.of(run)
    assert view.status == "partial" and view.imported == 2 and view.failed == 1
