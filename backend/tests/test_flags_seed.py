"""Seed CLI logic tests — drive app.flags.seed.seed over an in-memory service."""

from __future__ import annotations

from typing import Any

import pytest

from app.flags.defaults import default_experiments, default_flags
from app.flags.seed import seed
from app.flags.service import InMemoryFlagService

pytestmark = pytest.mark.asyncio


class _StubContainer:
    """Minimal stand-in exposing only ``flag_service`` (what seed() touches)."""

    def __init__(self) -> None:
        self.flag_service = InMemoryFlagService(default_salt="kinora")


async def test_seed_writes_all_definitions() -> None:
    container = _StubContainer()
    result = await seed(container, dry_run=False)  # type: ignore[arg-type]
    expected_flags = {f"flag:{f.key}" for f in default_flags()}
    expected_exps = {f"experiment:{e.key}" for e in default_experiments()}
    assert expected_flags <= set(result["written"])
    assert expected_exps <= set(result["written"])
    assert result["skipped"] == []


async def test_seed_dry_run_writes_nothing() -> None:
    container = _StubContainer()
    result = await seed(container, dry_run=True)  # type: ignore[arg-type]
    assert all("dry-run" in item for item in result["written"])
    # nothing persisted
    assert await container.flag_service.evaluate("live-video", _ctx()) is not None
    # the flag is absent (dry run) -> evaluate returns FLAG_NOT_FOUND default
    ev = await container.flag_service.evaluate("live-video", _ctx(), default="dflt")
    assert ev.value == "dflt"


async def test_seed_skip_existing() -> None:
    container = _StubContainer()
    await seed(container, dry_run=False)  # type: ignore[arg-type]
    result = await seed(container, skip_existing=True)  # type: ignore[arg-type]
    # second run with skip_existing should skip everything
    assert result["written"] == []
    assert len(result["skipped"]) == len(default_flags()) + len(default_experiments())


def _ctx() -> Any:
    from app.flags.context import EvalContext

    return EvalContext.of("u")
