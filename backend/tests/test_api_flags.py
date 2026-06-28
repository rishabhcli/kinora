"""Feature-flags API tests over the real gateway (require throwaway infra).

These drive the ``/api/flags`` surface end-to-end through the authenticated
gateway against the test container's Postgres + Redis. They skip cleanly when
the gateway infra is not configured (the shared ``api_client`` fixture skips).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.flags.models import Flag
from app.flags.serialization import flag_to_dict
from tests.conftest import requires_infra

pytestmark = [pytest.mark.asyncio, requires_infra]


def _bool_flag_body(key: str, *, enabled: bool = True, rollout: float | None = None) -> dict:
    return flag_to_dict(Flag.boolean(key, enabled=enabled, rollout_percent=rollout))


async def test_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/flags")
    assert resp.status_code == 401


async def test_upsert_get_and_list_flag(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    body = _bool_flag_body("live-video", enabled=True, rollout=100.0)
    resp = await api_client.put("/api/flags/live-video", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1

    got = await api_client.get("/api/flags/live-video", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["key"] == "live-video"

    listing = await api_client.get("/api/flags", headers=auth_headers)
    assert listing.status_code == 200
    assert "live-video" in {f["key"] for f in listing.json()}


async def test_evaluate_flag(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.put(
        "/api/flags/feat",
        json=_bool_flag_body("feat", enabled=True, rollout=100.0),
        headers=auth_headers,
    )
    resp = await api_client.post(
        "/api/flags/feat/evaluate",
        json={"context": {"key": "reader-1"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["value"] is True
    assert payload["reason"] == "fallthrough"


async def test_evaluate_missing_flag_returns_default(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/flags/ghost/evaluate",
        json={"context": {"key": "u"}, "default": "fallback"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == "fallback"
    assert resp.json()["reason"] == "flag_not_found"


async def test_toggle_kill_switch(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.put(
        "/api/flags/k",
        json=_bool_flag_body("k", enabled=True, rollout=100.0),
        headers=auth_headers,
    )
    off = await api_client.post(
        "/api/flags/k/enabled", json={"enabled": False}, headers=auth_headers
    )
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    # evaluation now serves the default (off) variation
    ev = await api_client.post(
        "/api/flags/k/evaluate", json={"context": {"key": "u"}}, headers=auth_headers
    )
    assert ev.json()["value"] is False
    assert ev.json()["reason"] == "flag_off"


async def test_targeting_rule_via_attributes(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    flag = {
        "key": "ladder",
        "kind": "string",
        "variations": [
            {"key": "full", "value": "full"},
            {"key": "kb", "value": "kenburns"},
        ],
        "default_variation": "kb",
        "fallthrough": {"weights": [{"variation": "kb", "weight": 10000}]},
        "rules": [
            {
                "id": "pro",
                "clauses": [{"attribute": "plan", "op": "eq", "values": ["pro"]}],
                "variation": "full",
            }
        ],
    }
    resp = await api_client.put("/api/flags/ladder", json=flag, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    pro = await api_client.post(
        "/api/flags/ladder/evaluate",
        json={"context": {"key": "u", "attributes": {"plan": "pro"}}},
        headers=auth_headers,
    )
    assert pro.json()["value"] == "full"
    free = await api_client.post(
        "/api/flags/ladder/evaluate",
        json={"context": {"key": "u", "attributes": {"plan": "free"}}},
        headers=auth_headers,
    )
    assert free.json()["value"] == "kenburns"


async def test_malformed_flag_rejected(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.put(
        "/api/flags/bad",
        json={"key": "bad", "kind": "boolean"},  # missing variations etc.
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "flag_invalid"


async def test_archive_and_audit(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.put(
        "/api/flags/temp",
        json=_bool_flag_body("temp", enabled=True),
        headers=auth_headers,
    )
    arch = await api_client.post("/api/flags/temp/archive", headers=auth_headers)
    assert arch.status_code == 200
    assert arch.json()["archived"] is True
    audit = await api_client.get("/api/flags/temp/audit", headers=auth_headers)
    assert audit.status_code == 200
    actions = {row["action"] for row in audit.json()}
    assert "create" in actions
    assert "archive" in actions


async def test_experiment_upsert_assign_and_exposures(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    exp = {
        "key": "crew-vs-baseline",
        "salt": "ccs",
        "status": "running",
        "variants": [
            {"key": "baseline", "weight": 5000, "is_control": True},
            {"key": "crew", "weight": 5000},
        ],
        "metrics": [{"key": "ccs"}],
    }
    resp = await api_client.put(
        "/api/flags/experiments/crew-vs-baseline", json=exp, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text

    for i in range(40):
        a = await api_client.post(
            "/api/flags/experiments/crew-vs-baseline/assign",
            json={"context": {"key": f"reader-{i}"}},
            headers=auth_headers,
        )
        assert a.status_code == 200
        assert a.json()["in_experiment"] is True
        assert a.json()["variant_key"] in ("baseline", "crew")

    counts = await api_client.get(
        "/api/flags/experiments/crew-vs-baseline/exposures", headers=auth_headers
    )
    assert counts.status_code == 200
    assert sum(counts.json().values()) == 40

    listing = await api_client.get("/api/flags/experiments/all", headers=auth_headers)
    assert "crew-vs-baseline" in {e["key"] for e in listing.json()}


async def test_experiment_decide_report(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    exp = {
        "key": "crew-vs-baseline",
        "salt": "ccs",
        "status": "running",
        "variants": [
            {"key": "baseline", "weight": 5000, "is_control": True},
            {"key": "crew", "weight": 5000},
        ],
        "metrics": [
            {"key": "ccs_pass", "kind": "proportion", "direction": "increase"},
            {
                "key": "regen_rate",
                "kind": "proportion",
                "direction": "decrease",
                "is_guardrail": True,
                "guardrail_margin": 0.1,
            },
        ],
    }
    await api_client.put(
        "/api/flags/experiments/crew-vs-baseline", json=exp, headers=auth_headers
    )
    resp = await api_client.post(
        "/api/flags/experiments/crew-vs-baseline/decide",
        json={
            "observations": {
                "baseline": {
                    "ccs_pass": {"successes": 1200, "trials": 2000},
                    "regen_rate": {"successes": 300, "trials": 2000},
                },
                "crew": {
                    "ccs_pass": {"successes": 1700, "trials": 2000},
                    "regen_rate": {"successes": 280, "trials": 2000},
                },
            }
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recommendation"] == "ship"


async def test_assign_unknown_experiment_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.post(
        "/api/flags/experiments/nope/assign",
        json={"context": {"key": "u"}},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_evaluate_all(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await api_client.put(
        "/api/flags/a",
        json=_bool_flag_body("a", enabled=True, rollout=100.0),
        headers=auth_headers,
    )
    await api_client.put(
        "/api/flags/b",
        json=_bool_flag_body("b", enabled=False),
        headers=auth_headers,
    )
    resp = await api_client.post(
        "/api/flags/evaluate-all",
        json={"context": {"key": "reader"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["a"]["value"] is True
    assert body["b"]["value"] is False
