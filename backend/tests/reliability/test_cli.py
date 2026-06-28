"""Unit tests for the loadtest CLI argument handling (loadtest.__main__).

These import the top-level ``loadtest`` package (which lives at the repo root,
beside ``backend/``) by prepending the repo root to ``sys.path``, then exercise
``main()`` in modes that issue **no** network traffic: ``--list-profiles``,
``--dry-run``, and the missing-target guard. A real run (which would build an
HttpxTransport) is never invoked here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The repo root is two levels up from this test file's backend/tests/reliability.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

loadtest_main = pytest.importorskip("loadtest.__main__")
canary_cli = pytest.importorskip("loadtest.canary_cli")
capacity_cli = pytest.importorskip("loadtest.capacity_cli")


def test_list_profiles_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = loadtest_main.main(["--list-profiles"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "steady_soak" in out
    assert "open_spike" in out


def test_dry_run_prints_plan_no_traffic(capsys: pytest.CaptureFixture[str]) -> None:
    rc = loadtest_main.main(
        ["--profile", "steady_soak", "--users", "8", "--duration", "30", "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert '"profile": "steady_soak"' in out
    assert '"kind": "closed"' in out


def test_dry_run_open_profile_reports_expected_arrivals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = loadtest_main.main(
        ["--profile", "open_spike", "--rps", "10", "--duration", "20", "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "expected_arrivals" in out
    assert '"rate_rps": 10' in out


def test_missing_target_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = loadtest_main.main(["--profile", "steady_soak"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--target is required" in err


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        loadtest_main.main(["--profile", "does_not_exist", "--dry-run"])


def test_canary_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    rc = canary_cli.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"journey": "kinora_read"' in out
    assert "login" in out


def test_canary_missing_target_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = canary_cli.main([])
    assert rc == 2
    assert "--target is required" in capsys.readouterr().err


def test_capacity_cli_is_offline(capsys: pytest.CaptureFixture[str]) -> None:
    # The capacity planner needs no target and issues no traffic.
    rc = capacity_cli.main(["--readers", "50", "--render-latency-s", "60"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"recommended_committed_slots"' in out
    assert '"feasible"' in out


def test_capacity_plan_structure() -> None:
    import argparse

    args = argparse.Namespace(
        readers=10,
        velocity_wps=4.0,
        active_fraction=0.7,
        render_latency_s=45.0,
        seconds_per_shot=5.0,
        max_utilisation=0.8,
        budget_video_s=1650.0,
        cache_hit_ratio=0.0,
        session_s=300.0,
        high_watermark_s=75.0,
    )
    doc = capacity_cli.plan(args)
    assert doc["workers"]["recommended_committed_slots"] >= 1
    assert doc["watermark"]["feasible"] in (True, False)
    assert doc["demand"]["arrival_rate_shots_per_s"] > 0
