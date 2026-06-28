"""Telemetry: the §13 per-agent quality/cost warehouse."""

from __future__ import annotations

import threading

from app.telemetry.warehouse import (
    CREW_ROLES,
    OTHER_ROLE,
    AgentStats,
    MetricsWarehouse,
    normalize_role,
)


def test_normalize_role_maps_known_and_unknown() -> None:
    for role in CREW_ROLES:
        assert normalize_role(role) == role
        assert normalize_role(role.upper()) == role
    assert normalize_role("mystery") == OTHER_ROLE
    assert normalize_role(None) == OTHER_ROLE
    assert normalize_role("") == OTHER_ROLE


def test_record_agent_call_accumulates() -> None:
    wh = MetricsWarehouse()
    wh.record_agent_call(
        "generator", latency_s=0.5, input_tokens=10, output_tokens=20, cost_usd=0.1
    )
    wh.record_agent_call(
        "generator", latency_s=1.5, input_tokens=5, output_tokens=5, cost_usd=0.2, repaired=True
    )
    stats = wh.agent("generator")
    assert stats is not None
    assert stats.calls == 2
    assert stats.input_tokens == 15
    assert stats.output_tokens == 25
    assert stats.total_tokens == 40
    assert abs(stats.cost_usd - 0.3) < 1e-9
    assert stats.repairs == 1
    assert abs(stats.repair_rate - 0.5) < 1e-9
    assert abs(stats.mean_latency_s - 1.0) < 1e-9


def test_error_rate_and_acceptance_rate() -> None:
    wh = MetricsWarehouse()
    wh.record_agent_call("critic", error=True)
    wh.record_agent_call("critic")
    stats = wh.agent("critic")
    assert stats is not None
    assert abs(stats.error_rate - 0.5) < 1e-9

    wh.record_shot_outcome("generator", accepted=True)
    wh.record_shot_outcome("generator", accepted=True)
    wh.record_shot_outcome("generator", accepted=False, regenerations=2, video_seconds=5.0)
    gen = wh.agent("generator")
    assert gen is not None
    assert gen.shots_accepted == 2
    assert gen.shots_degraded == 1
    assert gen.regenerations == 2
    assert abs(gen.video_seconds - 5.0) < 1e-9
    assert abs((gen.acceptance_rate or 0) - (2 / 3)) < 1e-9


def test_qa_means() -> None:
    wh = MetricsWarehouse()
    wh.record_qa("generator", ccs=0.9, style_drift=0.05, motion=0.1)
    wh.record_qa("generator", ccs=0.8, style_drift=0.07)
    gen = wh.agent("generator")
    assert gen is not None
    assert abs((gen.mean_ccs or 0) - 0.85) < 1e-9
    assert abs((gen.mean_style_drift or 0) - 0.06) < 1e-9
    assert abs((gen.mean_motion or 0) - 0.1) < 1e-9


def test_latency_percentiles_are_monotone() -> None:
    stats = AgentStats(role="generator")
    for v in [0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 2.0]:
        stats.latency.add(v)
    p50 = stats.latency.percentile(0.5)
    p95 = stats.latency.percentile(0.95)
    assert p50 <= p95
    assert 0.1 <= p50 <= 2.0


def test_latency_reservoir_is_bounded() -> None:
    stats = AgentStats(role="critic")
    for v in range(1000):
        stats.latency.add(float(v))
    assert len(stats.latency.samples) <= stats.latency.cap


def test_snapshot_orders_agents_and_computes_totals_and_derived() -> None:
    wh = MetricsWarehouse()
    # Insert out of crew order; the snapshot must sort by the canonical pipeline.
    wh.record_agent_call("critic", input_tokens=1, output_tokens=1)
    wh.record_agent_call("showrunner", input_tokens=2, output_tokens=2)
    wh.record_shot_outcome("generator", accepted=True, video_seconds=3.0)
    wh.record_shot_outcome("generator", accepted=False, regenerations=1)
    wh.record_qa("generator", ccs=0.95)

    snap = wh.snapshot()
    roles = [a["role"] for a in snap["agents"]]
    # Sorted by the canonical §7 pipeline order
    # (showrunner→adapter→cinematographer→generator→critic→continuity), so
    # showrunner precedes generator precedes critic regardless of insertion order.
    assert roles.index("showrunner") < roles.index("generator") < roles.index("critic")

    totals = snap["crew_totals"]
    assert totals["calls"] == 2
    assert totals["total_tokens"] == 6
    assert totals["shots_accepted"] == 1
    assert totals["shots_degraded"] == 1
    assert totals["regenerations"] == 1

    derived = snap["derived"]
    assert derived["total_shots"] == 2
    assert abs(derived["acceptance_rate"] - 0.5) < 1e-9
    assert abs(derived["regen_rate"] - 0.5) < 1e-9
    assert abs(derived["mean_ccs"] - 0.95) < 1e-9


def test_snapshot_empty_is_well_formed() -> None:
    wh = MetricsWarehouse()
    snap = wh.snapshot()
    assert snap["agents"] == []
    assert snap["crew_totals"]["calls"] == 0
    assert snap["derived"]["total_shots"] == 0
    assert snap["derived"]["acceptance_rate"] is None


def test_reset_clears_state() -> None:
    wh = MetricsWarehouse()
    wh.record_agent_call("adapter")
    assert wh.agent("adapter") is not None
    wh.reset()
    assert wh.agent("adapter") is None


def test_agent_returns_a_copy_not_the_live_object() -> None:
    wh = MetricsWarehouse()
    wh.record_agent_call("adapter", input_tokens=5)
    copy1 = wh.agent("adapter")
    assert copy1 is not None
    copy1.input_tokens = 999  # mutating the copy must not change the warehouse
    copy2 = wh.agent("adapter")
    assert copy2 is not None
    assert copy2.input_tokens == 5


def test_warehouse_is_thread_safe_under_concurrency() -> None:
    wh = MetricsWarehouse()

    def worker() -> None:
        for _ in range(500):
            wh.record_agent_call("generator", input_tokens=1, output_tokens=1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats = wh.agent("generator")
    assert stats is not None
    assert stats.calls == 8 * 500
    assert stats.input_tokens == 8 * 500


def test_to_dict_is_json_safe_and_complete() -> None:
    import json

    wh = MetricsWarehouse()
    wh.record_agent_call(
        "generator", latency_s=0.5, input_tokens=10, output_tokens=20, cost_usd=0.1
    )
    wh.record_qa("generator", ccs=0.9)
    wh.record_shot_outcome("generator", accepted=True, video_seconds=5.0)
    snap = wh.snapshot()
    # Round-trips cleanly through JSON (the read endpoint contract).
    encoded = json.dumps(snap)
    assert "generator" in encoded
    agent = snap["agents"][0]
    assert "latency" in agent and "quality" in agent
