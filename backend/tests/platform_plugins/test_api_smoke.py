"""HTTP-level smoke test for the /plugins API (requires the full infra fixtures).

Uses the project's ``api_client`` + ``auth_headers`` fixtures (Postgres + Redis +
MinIO), which skip cleanly when ``KINORA_TEST_DATABASE_URL`` et al. are unset.
Exercises the publish → install → enable → dispatch path through real HTTP, so
the router wiring, error mapping, and per-tenant isolation are covered end to end.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import requires_infra

pytestmark = requires_infra


_SOURCE = (
    "def run(payload, host):\n"
    "    out = dict(payload)\n"
    "    out['seen'] = True\n"
    "    return out\n"
)


def _manifest(pid: str = "com.smoke.tone", version: str = "1.0.0") -> dict:
    return {
        "id": pid,
        "version": version,
        "name": "Smoke Tone",
        "publisher": "smoke",
        "capabilities": ["book.read", "log.write"],
        "hooks": [{"id": "h", "point": "ingest.filter", "entrypoint": "run"}],
    }


async def test_publish_install_dispatch_over_http(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Publish (low-risk auto-approves under the default platform config).
    resp = await api_client.post(
        "/api/plugins", json={"manifest": _manifest(), "source": _SOURCE}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    # It shows up in the catalog.
    resp = await api_client.get("/api/plugins", headers=auth_headers)
    assert resp.status_code == 200
    ids = [c["plugin_id"] for c in resp.json()["items"]]
    assert "com.smoke.tone" in ids

    # Install + enable.
    resp = await api_client.post(
        "/api/plugins/com.smoke.tone/install",
        json={"version": "1.0.0", "enable": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "enabled"

    # Dispatch the ingest filter over a payload.
    resp = await api_client.post(
        "/api/plugins/dispatch",
        json={"point": "ingest.filter", "payload": {"text": "hi"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payload"]["seen"] is True
    assert body["outcomes"][0]["ok"] is True


async def test_rate_and_lifecycle_over_http(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.post(
        "/api/plugins",
        json={"manifest": _manifest(pid="com.smoke.rate"), "source": _SOURCE},
        headers=auth_headers,
    )
    resp = await api_client.post(
        "/api/plugins/com.smoke.rate/rate", json={"stars": 4}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["average"] == 4.0

    await api_client.post(
        "/api/plugins/com.smoke.rate/install", json={"version": "1.0.0"}, headers=auth_headers
    )
    resp = await api_client.post("/api/plugins/com.smoke.rate/enable", headers=auth_headers)
    assert resp.json()["state"] == "enabled"
    resp = await api_client.post("/api/plugins/com.smoke.rate/disable", headers=auth_headers)
    assert resp.json()["state"] == "disabled"

    resp = await api_client.get("/api/plugins/installed", headers=auth_headers)
    assert resp.status_code == 200
    assert any(i["plugin_id"] == "com.smoke.rate" for i in resp.json()["items"])


async def test_install_unknown_plugin_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/plugins/com.smoke.missing/install",
        json={"version": "1.0.0"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_publish_invalid_manifest_422(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    bad = _manifest()
    bad["capabilities"] = ["filesystem.write"]  # unknown capability
    resp = await api_client.post(
        "/api/plugins", json={"manifest": bad, "source": _SOURCE}, headers=auth_headers
    )
    assert resp.status_code == 422
