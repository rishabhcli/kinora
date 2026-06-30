"""Introspection API tests via TestClient against the router only (infra-free)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import install_exception_handlers
from app.video.registry.api import get_registry, router
from app.video.registry.catalog import load_catalog_text
from app.video.registry.registry import VideoProviderRegistry

_CATALOG_YAML = """
version: 1
providers:
  - id: incumbent
    kind: frontier
    display_name: Incumbent
    weight: 95
    rollout: ga
    cost_tier: turbo
    tags: [wan]
    capabilities:
      modes: [t2v, i2v, r2v]
      resolutions: [480P, 720P, 1080P]
      min_duration_s: 3
      max_duration_s: 10
  - id: canary
    kind: frontier
    weight: 5
    rollout: canary
    cost_tier: quality
    capabilities:
      modes: [i2v, r2v]
      resolutions: [720P, 1080P]
      min_duration_s: 3
      max_duration_s: 10
      supports_audio: true
  - id: off-model
    kind: open
    enabled: false
    weight: 50
    capabilities:
      modes: [t2v]
      resolutions: [720P]
"""


def _client() -> TestClient:
    registry = VideoProviderRegistry(load_catalog_text(_CATALOG_YAML))
    app = FastAPI()
    install_exception_handlers(app)  # so APIError -> JSON envelope
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_registry] = lambda: registry
    return TestClient(app)


# --------------------------------------------------------------------------- #
# GET /video/providers
# --------------------------------------------------------------------------- #


def test_list_providers_returns_all_with_effective_state() -> None:
    resp = _client().get("/api/video/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["incumbent"]["display_name"] == "Incumbent"
    assert by_id["incumbent"]["capabilities"]["max_resolution"] == "1080P"
    assert by_id["incumbent"]["routable"] is True
    # Disabled model reports enabled=False and zero effective weight.
    assert by_id["off-model"]["enabled"] is False
    assert by_id["off-model"]["weight"] == 0.0
    assert by_id["off-model"]["routable"] is False


def test_list_providers_enabled_only_and_routable_only() -> None:
    client = _client()
    enabled = client.get("/api/video/providers", params={"enabled_only": True}).json()
    assert {p["id"] for p in enabled["providers"]} == {"incumbent", "canary"}
    routable = client.get("/api/video/providers", params={"routable_only": True}).json()
    assert {p["id"] for p in routable["providers"]} == {"incumbent", "canary"}


def test_list_providers_kind_filter() -> None:
    body = _client().get("/api/video/providers", params={"kind": "open"}).json()
    assert {p["id"] for p in body["providers"]} == {"off-model"}


# --------------------------------------------------------------------------- #
# GET /video/providers/{id}
# --------------------------------------------------------------------------- #


def test_get_provider_found() -> None:
    resp = _client().get("/api/video/providers/canary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "canary"
    assert body["rollout"] == "canary"
    assert body["capabilities"]["supports_audio"] is True


def test_get_provider_unknown_is_404_envelope() -> None:
    resp = _client().get("/api/video/providers/ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "provider_not_found"


# --------------------------------------------------------------------------- #
# GET /video/capabilities
# --------------------------------------------------------------------------- #


def test_capabilities_query_returns_matches_and_split() -> None:
    resp = _client().get(
        "/api/video/capabilities",
        params={"mode": "r2v", "resolution": "720P", "duration": 8},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["provider"]["id"] for m in body["matches"]]
    assert ids == ["incumbent", "canary"]  # best-weighted first
    shares = {m["provider"]["id"]: m["expected_share"] for m in body["matches"]}
    assert abs(shares["incumbent"] - 0.95) < 1e-9
    assert abs(shares["canary"] - 0.05) < 1e-9
    assert body["query"]["mode"] == "r2v"


def test_capabilities_query_long_form_mode() -> None:
    body = _client().get(
        "/api/video/capabilities", params={"mode": "reference_to_video"}
    ).json()
    assert {m["provider"]["id"] for m in body["matches"]} == {"incumbent", "canary"}


def test_capabilities_query_require_audio() -> None:
    body = _client().get(
        "/api/video/capabilities", params={"require_audio": True}
    ).json()
    assert [m["provider"]["id"] for m in body["matches"]] == ["canary"]


def test_capabilities_unknown_mode_is_400() -> None:
    resp = _client().get("/api/video/capabilities", params={"mode": "hologram"})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_query"


def test_capabilities_no_match_returns_empty() -> None:
    resp = _client().get("/api/video/capabilities", params={"resolution": "2160P"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["matches"] == []


def test_default_registry_is_lazily_built_when_not_overridden() -> None:
    # No dependency override => the lazy default catalog is served.
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router, prefix="/api")
    resp = TestClient(app).get("/api/video/providers")
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()["providers"]}
    assert "wan2.1-t2v-turbo" in ids
