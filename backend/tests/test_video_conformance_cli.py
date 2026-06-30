"""Tests for the ``python -m app.video.conformance`` CLI entry point."""

from __future__ import annotations

import pytest

from app.video.conformance.__main__ import (
    available_targets,
    main,
    register_provider,
)
from app.video.conformance.fakes import BROKEN_BEHAVIOURS, fake_kit


def test_reference_target_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["reference"])
    assert code == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "reference" in out


def test_quiet_prints_only_summary(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["reference", "--quiet"])
    assert code == 0
    # The CLI prints exactly one verdict line (structlog may add operational
    # lines above it); --quiet means no per-check report rows.
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    verdict = lines[-1]
    assert verdict.startswith("[PASS]")
    # No per-check rows (those are indented "  PASS  <check>" lines).
    assert not any(ln.lstrip().startswith(("PASS  ", "FAIL  ")) for ln in lines)


@pytest.mark.parametrize("name", sorted(BROKEN_BEHAVIOURS))
def test_broken_targets_exit_one(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main([name])
    assert code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_unknown_provider_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["does-not-exist"])
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown provider" in err


def test_list_exits_zero_and_lists_targets(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "reference" in out
    for name in BROKEN_BEHAVIOURS:
        assert name in out


def test_no_args_lists_and_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 2
    assert "reference" in capsys.readouterr().out


def test_available_targets_includes_builtins() -> None:
    targets = available_targets()
    assert "reference" in targets
    assert set(BROKEN_BEHAVIOURS).issubset(set(targets))
    assert targets == sorted(targets)


def test_register_provider_adds_runnable_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    register_provider("custom-ref", lambda: fake_kit(name="custom-ref"))
    try:
        assert "custom-ref" in available_targets()
        code = main(["custom-ref", "--quiet"])
        assert code == 0
        assert "custom-ref" in capsys.readouterr().out
    finally:
        # Keep the module-global registry clean for other tests.
        from app.video.conformance.__main__ import _REGISTRY

        _REGISTRY.pop("custom-ref", None)
