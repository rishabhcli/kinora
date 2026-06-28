"""Integration test for the reports CLI (`python -m app.reports.run`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.composition import Container
from app.reports import run as cli

pytestmark = pytest.mark.asyncio


async def test_cli_writes_operator_report_to_file(
    container: Container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the CLI's composition root at the test container so it uses the
    # isolated infra rather than building a fresh one.
    monkeypatch.setattr(cli, "build_container", lambda _s: container)
    out = tmp_path / "budget.json"
    args = cli._parse_args(["--kind", "budget", "--format", "json", "--out", str(out)])
    rc = await cli.run_async(args)
    assert rc == 0
    data = out.read_bytes()
    assert b'"budget"' in data


async def test_cli_store_persists_artifact(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "build_container", lambda _s: container)
    args = cli._parse_args(["--kind", "library_overview", "--format", "html", "--store"])
    rc = await cli.run_async(args)
    assert rc == 0
    # The artifact row was indexed.
    from app.reports.db_model import ReportAudience
    from app.reports.repository import ReportArtifactRepo

    async with container.session_factory() as session:
        rows = await ReportArtifactRepo(session).list_by_audience(ReportAudience.OPERATOR)
    assert any(r.kind.value == "library_overview" for r in rows)
