"""Runtime-config admin API via TestClient — deterministic, no infra.

Mounts only the plane router on a bare FastAPI app and overrides the auth /
container / rate-limit dependencies with infra-free stubs, so the whole admin
surface (list, resolve, override, target, rollout, audit, import, kill-switch
rejection) is exercised end-to-end without Postgres / Redis / a real JWT.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_container, get_current_user, write_rate_limit
from app.api.errors import install_exception_handlers
from app.flags.plane.api import router as plane_router
from app.flags.plane.plane import RuntimeConfigPlane
from app.flags.plane.registry import build_default_registry


@dataclass
class _StubUser:
    id: str = "user-admin"


class _StubContainer:
    def __init__(self, plane: RuntimeConfigPlane) -> None:
        self.runtime_config_plane = plane


@pytest.fixture
def plane() -> RuntimeConfigPlane:
    return RuntimeConfigPlane(build_default_registry())


@pytest.fixture
def client(plane: RuntimeConfigPlane) -> Iterator[TestClient]:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(plane_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _StubUser()
    app.dependency_overrides[get_container] = lambda: _StubContainer(plane)
    app.dependency_overrides[write_rate_limit] = lambda: None
    with TestClient(app) as c:
        yield c


def test_list_flags(client: TestClient) -> None:
    resp = client.get("/api/runtime-config")
    assert resp.status_code == 200
    body = resp.json()
    keys = {f["key"] for f in body["flags"]}
    assert "kinora.live_video" in keys
    assert "video.backend" in keys
    assert "layer_version" in body


def test_get_single_flag(client: TestClient) -> None:
    resp = client.get("/api/runtime-config/video.backend")
    assert resp.status_code == 200
    body = resp.json()
    assert body["spec"]["type"] == "string"
    assert body["resolution"]["value"] == "dashscope"


def test_get_unknown_flag_404(client: TestClient) -> None:
    resp = client.get("/api/runtime-config/ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "flag_not_found"


def test_set_override_and_resolve(client: TestClient) -> None:
    resp = client.put(
        "/api/runtime-config/provider.gateway_enabled/override", json={"value": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] is True

    resolved = client.post(
        "/api/runtime-config/provider.gateway_enabled/resolve", json={"context": {}}
    )
    assert resolved.json()["value"] is True


def test_kill_switch_override_rejected_409(client: TestClient) -> None:
    # Forcing the live-video gate OFF is allowed.
    ok = client.put("/api/runtime-config/kinora.live_video/override", json={"value": False})
    assert ok.status_code == 200
    # Forcing it ON is rejected with a 409 kill-switch violation.
    bad = client.put("/api/runtime-config/kinora.live_video/override", json={"value": True})
    assert bad.status_code == 409
    assert bad.json()["error"]["type"] == "kill_switch_violation"


def test_type_error_returns_422(client: TestClient) -> None:
    resp = client.put(
        "/api/runtime-config/render.poison_threshold/override",
        json={"value": "not-an-int"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "flag_type_error"


def test_targeting_rule_via_api(client: TestClient) -> None:
    add = client.post(
        "/api/runtime-config/video.backend/rules",
        json={"id": "beta", "value": "minimax", "cohort": "beta"},
    )
    assert add.status_code == 200, add.text

    beta = client.post(
        "/api/runtime-config/video.backend/resolve",
        json={"context": {"cohort": "beta"}},
    )
    assert beta.json()["value"] == "minimax"
    ga = client.post(
        "/api/runtime-config/video.backend/resolve",
        json={"context": {"cohort": "ga"}},
    )
    assert ga.json()["value"] == "dashscope"

    removed = client.delete("/api/runtime-config/video.backend/rules/beta")
    assert removed.status_code == 200
    after = client.post(
        "/api/runtime-config/video.backend/resolve",
        json={"context": {"cohort": "beta"}},
    )
    assert after.json()["value"] == "dashscope"


def test_rollout_via_api_and_kill_switch_rollout_rejected(client: TestClient) -> None:
    ok = client.put(
        "/api/runtime-config/analytics.enabled/rollout",
        json={"percent": 100.0, "bucket_by": "user"},
    )
    assert ok.status_code == 200
    resolved = client.post(
        "/api/runtime-config/analytics.enabled/resolve",
        json={"context": {"user": "u1"}},
    )
    assert resolved.json()["value"] is True

    # A rollout on the guarded kill-switch is refused.
    bad = client.put(
        "/api/runtime-config/kinora.live_video/rollout", json={"percent": 50.0}
    )
    assert bad.status_code == 409


def test_snapshot_endpoint(client: TestClient) -> None:
    client.post(
        "/api/runtime-config/video.backend/rules",
        json={"id": "beta", "value": "minimax", "cohort": "beta"},
    )
    resp = client.post("/api/runtime-config/snapshot", json={"context": {"cohort": "beta"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["flags"]["video.backend"]["value"] == "minimax"


def test_audit_endpoint(client: TestClient) -> None:
    client.put("/api/runtime-config/provider.gateway_enabled/override", json={"value": True})
    resp = client.get("/api/runtime-config/audit")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert rows[0]["kind"] == "set_static"


def test_export_and_import_endpoints(client: TestClient) -> None:
    client.put("/api/runtime-config/provider.gateway_enabled/override", json={"value": True})
    exported = client.get("/api/runtime-config/overrides").json()
    assert "provider.gateway_enabled" in exported["overlays"]

    imported = client.post("/api/runtime-config/import", json=exported)
    assert imported.status_code == 200
    assert "provider.gateway_enabled" in imported.json()["overlays"]


def test_import_rejecting_kill_switch_raise_409(client: TestClient) -> None:
    bad = {
        "version": 0,
        "overlays": {
            "kinora.live_video": {"static": {"value": True}, "rules": [], "rollout": None}
        },
    }
    resp = client.post("/api/runtime-config/import", json=bad)
    assert resp.status_code == 409


def test_clear_flag_endpoint(client: TestClient) -> None:
    client.put("/api/runtime-config/provider.gateway_enabled/override", json={"value": True})
    resp = client.delete("/api/runtime-config/provider.gateway_enabled")
    assert resp.status_code == 200
    overrides = client.get("/api/runtime-config/overrides").json()
    assert overrides["overlays"] == {}
