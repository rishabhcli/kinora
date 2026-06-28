"""Telemetry: Prometheus rules-as-code + Grafana dashboards-as-code."""

from __future__ import annotations

import json

from app.telemetry import alerts, dashboards
from app.telemetry.slo import default_slos

# --------------------------------------------------------------------------- #
# Alerting + recording rules
# --------------------------------------------------------------------------- #


def test_recording_rules_one_per_slo_with_a_query() -> None:
    rules = alerts.build_recording_rules()
    group = rules["groups"][0]
    records = {r["record"] for r in group["rules"]}
    for slo in default_slos():
        if slo.sli_query:
            assert f"kinora:slo_good_ratio:{slo.name}" in records


def test_alert_rules_emit_a_tier_per_burn_window() -> None:
    rules = alerts.build_alert_rules()
    by_group = {g["name"]: g for g in rules["groups"]}
    # api_availability has the standard four-tier ladder.
    group = by_group["kinora_slo_api_availability"]
    alert_names = {r["alert"] for r in group["rules"]}
    assert "KinoraSLO_api_availability_fast_burn" in alert_names
    assert "KinoraSLO_api_availability_trickle_burn" in alert_names
    assert len(group["rules"]) == 4


def test_alert_expr_uses_both_windows_and_the_burn_rate() -> None:
    rules = alerts.build_alert_rules()
    group = next(g for g in rules["groups"] if g["name"] == "kinora_slo_api_availability")
    fast = next(r for r in group["rules"] if r["alert"].endswith("fast_burn"))
    # The fast tier ANDs a 1h and a 5m window at 14.4x.
    assert "[1h]" in fast["expr"]
    assert "[5m]" in fast["expr"]
    assert "14.4" in fast["expr"]
    assert " and " in fast["expr"]
    assert fast["labels"]["severity"] == "page"
    assert fast["labels"]["slo"] == "api_availability"


def test_slos_without_burn_windows_emit_no_alert_group() -> None:
    rules = alerts.build_alert_rules()
    group_names = {g["name"] for g in rules["groups"]}
    # ccs_quality has no burn windows (it's a floor, not an error budget).
    assert "kinora_slo_ccs_quality" not in group_names


def test_window_rewrite_replaces_default_ranges() -> None:
    rewritten = alerts._with_window("rate(x[5m]) / rate(y[15m])", "1h")
    assert rewritten == "rate(x[1h]) / rate(y[1h])"


def test_rules_yaml_is_parseable_and_complete() -> None:
    text = alerts.rules_yaml()
    assert text.endswith("\n")
    assert "groups:" in text
    assert "KinoraSLO_api_availability_fast_burn" in text
    # The record name carries a colon, so the emitter must quote it (valid YAML).
    assert 'record: "kinora:slo_good_ratio:api_availability"' in text
    assert "expr:" in text


def test_yaml_emitter_quotes_special_scalars() -> None:
    out = alerts.to_yaml({"a": "plain", "b": "has: colon", "c": True, "d": 3})
    assert "a: plain" in out
    assert 'b: "has: colon"' in out
    assert "c: true" in out
    assert "d: 3" in out


def test_yaml_roundtrips_through_pyyaml_if_available() -> None:
    # PyYAML is a transitive dependency; assert our emitter produces valid YAML.
    import yaml

    parsed = yaml.safe_load(alerts.rules_yaml())
    assert "groups" in parsed
    names = {g["name"] for g in parsed["groups"]}
    assert "kinora_slo_api_availability" in names


# --------------------------------------------------------------------------- #
# Dashboards
# --------------------------------------------------------------------------- #


def test_dashboard_names() -> None:
    assert set(dashboards.dashboard_names()) == {"overview", "crew"}


def test_overview_dashboard_has_red_and_use_panels() -> None:
    d = dashboards.build_dashboard("overview")
    assert d is not None
    titles = " ".join(p["title"] for p in d["panels"])
    assert "RED: Rate" in titles
    assert "RED: Errors" in titles
    assert "RED: Duration" in titles
    assert "USE: Saturation" in titles
    assert "USE: Utilization" in titles
    assert d["uid"] == "kinora-overview"


def test_crew_dashboard_has_per_agent_panels() -> None:
    d = dashboards.build_dashboard("crew")
    assert d is not None
    exprs = " ".join(t["expr"] for p in d["panels"] for t in p["targets"])
    assert "kinora_agent_cost_usd_gauge" in exprs
    assert "kinora_agent_latency_p95_seconds" in exprs
    assert "kinora_agent_mean_ccs_gauge" in exprs


def test_unknown_dashboard_returns_none() -> None:
    assert dashboards.build_dashboard("nope") is None


def test_dashboards_have_unique_panel_ids() -> None:
    for name in dashboards.dashboard_names():
        d = dashboards.build_dashboard(name)
        assert d is not None
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids)), f"{name} has duplicate panel ids"


def test_all_dashboards_are_json_safe() -> None:
    encoded = json.dumps(dashboards.all_dashboards())
    assert "kinora-overview" in encoded
    assert "kinora-crew" in encoded


def test_build_dashboard_is_stable_across_calls() -> None:
    a = dashboards.build_dashboard("overview")
    b = dashboards.build_dashboard("overview")
    assert a == b  # rebuilding resets panel ids deterministically
