"""Conformance gate: good plugin passes, broken/mismatched plugins fail."""

from __future__ import annotations

from typing import Any

from app.video.plugins.conformance import (
    DEFAULT_CONTRACT,
    ConformanceCase,
    ConformanceHarness,
)
from app.video.plugins.contracts import (
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoRequest,
)
from app.video.plugins.limits import ResourceLimits
from app.video.plugins.sandbox import CapabilityGrant, HostServices, Sandbox

from .conftest import BrokenGeneratePlugin, GoodPlugin, WrongModePlugin

_PROFILE = GoodPlugin.capabilities


def _sandbox() -> Sandbox:
    return Sandbox(
        plugin_id="com.test.p",
        grant=CapabilityGrant(frozenset()),
        services=HostServices(),
        limits=ResourceLimits(),
    )


async def test_good_plugin_passes_all_required_cases() -> None:
    plugin = GoodPlugin(config={}, host=object())
    report = await ConformanceHarness().run(
        plugin_ref="com.acme.good@1.0.0",
        plugin=plugin,
        profile=_PROFILE,
        sandbox=_sandbox(),
    )
    assert report.passed
    assert report.failures == ()
    assert {r.name for r in report.results} == {c.name for c in DEFAULT_CONTRACT}


async def test_broken_generate_fails_conformance() -> None:
    plugin = BrokenGeneratePlugin(config={}, host=object())
    report = await ConformanceHarness().run(
        plugin_ref="com.acme.broken@1.0.0",
        plugin=plugin,
        profile=_PROFILE,
        sandbox=_sandbox(),
    )
    assert not report.passed
    assert "generate_honours_request" in report.failures


async def test_wrong_mode_fails_conformance() -> None:
    plugin = WrongModePlugin(config={}, host=object())
    report = await ConformanceHarness().run(
        plugin_ref="com.acme.wrong@1.0.0",
        plugin=plugin,
        profile=_PROFILE,
        sandbox=_sandbox(),
    )
    assert not report.passed
    assert "generate_honours_request" in report.failures


async def test_runtime_profile_mismatch_fails() -> None:
    plugin = GoodPlugin(config={}, host=object())
    # Declare a profile the runtime object does not match.
    other = CapabilityProfile(modes=frozenset({RenderMode.IMAGE_TO_VIDEO}), max_reference_images=0)
    report = await ConformanceHarness().run(
        plugin_ref="x@1.0.0", plugin=plugin, profile=other, sandbox=_sandbox()
    )
    assert "capabilities_match" in report.failures


async def test_self_inconsistent_profile_fails() -> None:
    # r2v advertised but max_reference_images == 0 is incoherent.
    class R2VPlugin:
        capabilities = CapabilityProfile(
            modes=frozenset({RenderMode.REFERENCE_TO_VIDEO}), max_reference_images=0
        )

        async def probe(self) -> ProbeResult:
            return ProbeResult(healthy=True)

        async def generate(self, request: VideoRequest) -> VideoArtifact:
            return VideoArtifact(clip_url="x", duration_s=1.0, model="m", mode=request.mode)

    plugin = R2VPlugin()
    report = await ConformanceHarness().run(
        plugin_ref="x@1.0.0",
        plugin=plugin,
        profile=plugin.capabilities,
        sandbox=_sandbox(),
    )
    assert "profile_self_consistent" in report.failures


async def test_optional_case_failure_tolerated() -> None:
    async def always_fail(_p: Any, _prof: Any, _sb: Any) -> tuple[bool, str]:
        return False, "intentional optional failure"

    harness = ConformanceHarness(extra=(ConformanceCase("opt", required=False, check=always_fail),))
    plugin = GoodPlugin(config={}, host=object())
    report = await harness.run(
        plugin_ref="x@1.0.0", plugin=plugin, profile=_PROFILE, sandbox=_sandbox()
    )
    # The required contract still passes; the optional failure is recorded only.
    assert report.passed
    assert any(r.name == "opt" and not r.passed for r in report.results)


async def test_throwing_case_is_a_failure_not_a_crash() -> None:
    async def boom(_p: Any, _prof: Any, _sb: Any) -> tuple[bool, str]:
        raise RuntimeError("case bug")

    harness = ConformanceHarness(extra=(ConformanceCase("boom", required=True, check=boom),))
    plugin = GoodPlugin(config={}, host=object())
    report = await harness.run(
        plugin_ref="x@1.0.0", plugin=plugin, profile=_PROFILE, sandbox=_sandbox()
    )
    assert "boom" in report.failures
