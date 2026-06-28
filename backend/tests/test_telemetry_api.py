"""Telemetry read API — warehouse / SLO / alerts / dashboards endpoints.

Driven through the Phase-9 app over httpx ``ASGITransport`` with the throwaway
infra fixtures (so it skips cleanly offline like the other gateway tests). These
endpoints touch no infra themselves, but they require an authenticated user, so
they ride the same ``api_client`` / ``auth_headers`` fixtures.
"""

from __future__ import annotations

import yaml
from httpx import AsyncClient

from app.telemetry.warehouse import get_warehouse


async def test_warehouse_endpoint_returns_rollup(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Seed some live activity into the process warehouse.
    wh = get_warehouse()
    wh.reset()
    wh.record_agent_call(
        "generator", latency_s=0.5, input_tokens=10, output_tokens=20, cost_usd=0.1
    )
    wh.record_qa("generator", ccs=0.92)
    wh.record_shot_outcome("generator", accepted=True, video_seconds=5.0)

    resp = await api_client.get("/api/eval/warehouse", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "agents" in body and "crew_totals" in body and "derived" in body
    gen = next(a for a in body["agents"] if a["role"] == "generator")
    assert gen["calls"] == 1
    assert gen["quality"]["mean_ccs"] == 0.92
    assert body["crew_totals"]["calls"] == 1


async def test_warehouse_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/eval/warehouse")
    assert resp.status_code in (401, 403)


async def test_slo_catalogue_endpoint(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/eval/slo", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {s["name"] for s in body["slos"]}
    assert {"api_availability", "render_job_success", "qa_pass_rate"} <= names


async def test_slo_alerts_json_and_yaml(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp_json = await api_client.get("/api/eval/slo/alerts", headers=auth_headers)
    assert resp_json.status_code == 200, resp_json.text
    groups = resp_json.json()["groups"]
    group_names = {g["name"] for g in groups}
    assert "kinora_slo_recording" in group_names
    assert "kinora_slo_api_availability" in group_names

    resp_yaml = await api_client.get(
        "/api/eval/slo/alerts", headers=auth_headers, params={"fmt": "yaml"}
    )
    assert resp_yaml.status_code == 200
    assert resp_yaml.headers["content-type"].startswith("text/plain")
    parsed = yaml.safe_load(resp_yaml.text)
    assert "groups" in parsed


async def test_slo_alerts_rejects_bad_fmt(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/eval/slo/alerts", headers=auth_headers, params={"fmt": "xml"})
    assert resp.status_code == 422


async def test_dashboards_list_and_fetch(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    listing = await api_client.get("/api/eval/dashboards", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    names = {d["name"] for d in listing.json()["dashboards"]}
    assert names == {"overview", "crew"}

    overview = await api_client.get("/api/eval/dashboards/overview", headers=auth_headers)
    assert overview.status_code == 200
    model = overview.json()
    assert model["uid"] == "kinora-overview"
    assert len(model["panels"]) > 0


async def test_unknown_dashboard_404s(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/eval/dashboards/nope", headers=auth_headers)
    assert resp.status_code == 404


async def test_warehouse_export_is_visible_on_metrics(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    wh = get_warehouse()
    wh.reset()
    wh.record_agent_call("critic", input_tokens=3, output_tokens=4, cost_usd=0.02)
    # Hitting the warehouse endpoint mirrors the rollup into the Prometheus gauges.
    await api_client.get("/api/eval/warehouse", headers=auth_headers)
    metrics = await api_client.get("/metrics")
    assert metrics.status_code == 200
    assert "kinora_agent_calls_total_gauge" in metrics.text
