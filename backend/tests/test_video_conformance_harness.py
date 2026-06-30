"""The harness self-test: the reference fake PASSES, every broken fake FAILS.

This is the keystone test of the whole subsystem — it proves the conformance
harness is *trustworthy*: it accepts a correct adapter and catches each specific
violation, one broken fake per check. No network, no spend, fully deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.video.conformance import (
    CheckOutcome,
    ConformanceCheck,
    run_conformance,
)
from app.video.conformance.fakes import (
    BROKEN_BEHAVIOURS,
    fake_kit,
    make_reference,
)
from app.video.conformance.suite import assert_conformant, run_provider_conformance

# --------------------------------------------------------------------------- #
# The reference fake passes every check
# --------------------------------------------------------------------------- #


async def test_reference_fake_passes_all_checks() -> None:
    kit = fake_kit(name="reference")
    report = await run_conformance(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )
    assert report.passed, report.render_text()
    assert report.score == 1.0
    assert not report.failures
    # Every check is represented exactly once.
    seen = {r.check for r in report.results}
    assert seen == set(ConformanceCheck)
    assert len(report.results) == len(ConformanceCheck)


async def test_reference_fake_no_skips_or_errors() -> None:
    """A fully-featured reference adapter exercises every check (no SKIP/ERROR)."""
    kit = fake_kit(name="reference")
    report = await run_conformance(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )
    outcomes = {r.check: r.outcome for r in report.results}
    assert all(o is CheckOutcome.PASS for o in outcomes.values()), outcomes


async def test_assert_conformant_passes_for_reference() -> None:
    kit = fake_kit(name="reference")
    # Must not raise.
    report = await assert_conformant(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )
    assert report.passed


# --------------------------------------------------------------------------- #
# Each broken fake fails EXACTLY its target check
# --------------------------------------------------------------------------- #

#: Maps each broken fake id to the single check the harness must flip to FAIL.
_EXPECTED_FAILURE: dict[str, ConformanceCheck] = {
    "broken-undeclared-mode": ConformanceCheck.CAPABILITY_HONESTY,
    "broken-any-duration": ConformanceCheck.CAPABILITY_HONESTY,
    "broken-drop-prompt": ConformanceCheck.REQUEST_MAPPING,
    "broken-taxonomy": ConformanceCheck.ERROR_TAXONOMY,
    "broken-no-download": ConformanceCheck.ASSET_HANDLING,
    "broken-no-last-frame": ConformanceCheck.LAST_FRAME,
    "broken-double-spend": ConformanceCheck.IDEMPOTENCY,
    "broken-ignore-cancel": ConformanceCheck.CANCELLATION,
    "broken-wrong-timeout": ConformanceCheck.TIMEOUT,
    "broken-spend-leak": ConformanceCheck.SPEND_GATE,
    "broken-declaration": ConformanceCheck.CAPABILITY_DECLARATION,
}


def test_every_broken_behaviour_has_an_expected_failure() -> None:
    """No broken fake is left untested (registry ↔ expectation parity)."""
    assert set(_EXPECTED_FAILURE) == set(BROKEN_BEHAVIOURS)


@pytest.mark.parametrize(("name", "expected"), sorted(_EXPECTED_FAILURE.items()))
async def test_broken_fake_fails_only_its_check(
    name: str, expected: ConformanceCheck
) -> None:
    kit = fake_kit(name=name, behaviour=BROKEN_BEHAVIOURS[name])
    report = await run_conformance(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )
    assert not report.passed, f"{name} should not pass conformance"
    failing = {r.check for r in report.failures}
    assert failing == {expected}, (
        f"{name}: expected only {expected.value} to fail, got "
        f"{sorted(c.value for c in failing)}\n{report.render_text()}"
    )


@pytest.mark.parametrize("name", sorted(BROKEN_BEHAVIOURS))
async def test_assert_conformant_raises_for_broken(name: str) -> None:
    kit = fake_kit(name=name, behaviour=BROKEN_BEHAVIOURS[name])
    with pytest.raises(AssertionError) as excinfo:
        await assert_conformant(
            kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
        )
    # The message names the provider and embeds the per-check report.
    assert name in str(excinfo.value)
    assert _EXPECTED_FAILURE[name].value in str(excinfo.value)


# --------------------------------------------------------------------------- #
# assert_conformant's `required` filter tolerates non-required failures
# --------------------------------------------------------------------------- #


async def test_required_filter_tolerates_unlisted_failures() -> None:
    name = "broken-no-last-frame"
    kit = fake_kit(name=name, behaviour=BROKEN_BEHAVIOURS[name])
    # The only failure is LAST_FRAME; require a different check → tolerated.
    report = await assert_conformant(
        kit.provider,
        rebuild=kit.rebuild,
        rebuild_gated=kit.rebuild_gated,
        required={ConformanceCheck.CAPABILITY_HONESTY},
    )
    # The report still records the real failure even though we tolerated it.
    assert not report.passed
    assert {r.check for r in report.failures} == {ConformanceCheck.LAST_FRAME}


async def test_required_filter_still_raises_for_listed_failure() -> None:
    name = "broken-taxonomy"
    kit = fake_kit(name=name, behaviour=BROKEN_BEHAVIOURS[name])
    with pytest.raises(AssertionError):
        await assert_conformant(
            kit.provider,
            rebuild=kit.rebuild,
            rebuild_gated=kit.rebuild_gated,
            required={ConformanceCheck.ERROR_TAXONOMY},
        )


# --------------------------------------------------------------------------- #
# Determinism + the suite alias
# --------------------------------------------------------------------------- #


async def test_run_is_deterministic() -> None:
    """Two runs of the same fake produce identical per-check outcomes."""
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    kit_a = fake_kit(name="reference")
    kit_b = fake_kit(name="reference")
    a = await run_conformance(
        kit_a.provider, rebuild=kit_a.rebuild, rebuild_gated=kit_a.rebuild_gated, now=fixed
    )
    b = await run_conformance(
        kit_b.provider, rebuild=kit_b.rebuild, rebuild_gated=kit_b.rebuild_gated, now=fixed
    )
    assert a.generated_at == b.generated_at == fixed
    assert [(r.check, r.outcome) for r in a.results] == [
        (r.check, r.outcome) for r in b.results
    ]


async def test_suite_alias_matches_runner() -> None:
    kit = fake_kit(name="reference")
    via_suite = await run_provider_conformance(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )
    assert via_suite.passed


# --------------------------------------------------------------------------- #
# A provider missing the required surface fails SURFACE and skips the rest
# --------------------------------------------------------------------------- #


class _NoRenderProvider:
    name = "no-render"

    def capabilities(self) -> object:  # pragma: no cover - shape probe only
        raise NotImplementedError


async def test_missing_surface_fails_surface_and_skips_rest() -> None:
    provider = _NoRenderProvider()
    report = await run_conformance(provider)  # type: ignore[arg-type]
    surface = report.result_for(ConformanceCheck.SURFACE)
    assert surface is not None and surface.outcome is CheckOutcome.FAIL
    # Async checks are skipped (not errored) when the surface is unusable.
    others = [r for r in report.results if r.check is not ConformanceCheck.SURFACE]
    assert all(
        r.outcome in (CheckOutcome.SKIP, CheckOutcome.FAIL) for r in others
    ), report.render_text()


# --------------------------------------------------------------------------- #
# Gate-open by default: render proceeds, spend gate honoured when closed
# --------------------------------------------------------------------------- #


async def test_reference_render_returns_real_bytes() -> None:
    provider = make_reference(live=True)
    from app.providers.types import WanMode, WanSpec

    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="hi", duration_s=5, resolution="720P")
    result = await provider.render(spec)
    assert result.clip_bytes and len(result.clip_bytes) > 0
    assert result.last_frame_bytes and len(result.last_frame_bytes) > 0
    assert result.mode is WanMode.TEXT_TO_VIDEO


async def test_reference_gate_closed_raises_without_spend() -> None:
    from app.providers.errors import LiveVideoDisabled
    from app.providers.types import WanMode, WanSpec

    provider = make_reference(live=False)
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="hi", duration_s=5, resolution="720P")
    with pytest.raises(LiveVideoDisabled):
        await provider.render(spec)
    assert provider.transport.submit_calls == 0


async def test_no_gated_factory_yields_skip_when_gate_open() -> None:
    """Without a gate-closed rebuild, the spend-gate check SKIPs (can't verify)."""
    kit = fake_kit(name="reference")
    report = await run_conformance(kit.provider, rebuild=kit.rebuild)  # no rebuild_gated
    spend = report.result_for(ConformanceCheck.SPEND_GATE)
    assert spend is not None and spend.outcome is CheckOutcome.SKIP
    # A SKIP never fails the verdict.
    assert report.passed
