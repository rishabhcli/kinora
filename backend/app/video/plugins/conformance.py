"""The conformance gate — prove a plugin honours the contract before activation.

Earlier rounds built a *conformance suite* that asserts a provider behaves
correctly (capabilities self-consistent, probe cheap, generate honours the
request, errors typed). Per the final-round rules that suite is not merged, so
this module defines a **minimal LOCAL conformance contract** mirroring it: a
list of named :class:`ConformanceCase` s the SDK runs against a freshly-loaded
plugin *through the sandbox*. The orchestrator swaps in the real, fuller
conformance suite at final integration — the gate's shape (a list of async
cases producing a :class:`ConformanceReport`) is the stable seam.

The gate is the SDK's quality firewall: a plugin that passes is activated; a
plugin that fails *any required case* is **quarantined** — kept in the registry
in a non-routable state with its failure record, never reaching a render path.
Because every case runs through :class:`~app.video.plugins.sandbox.Sandbox`, a
case can't be subverted by a plugin escaping its budget or capabilities: a
sandbox violation *is* a conformance failure.

Crucially, conformance runs **without network and without spend**: cases exercise
``probe`` and ``generate`` for modes the plugin advertises, but the host services
injected during conformance are *no-spend stubs* (a fetch that returns a canned
descriptor-shaped response, a usage sink that records nothing real). The gate
checks the *shape* of plugin behaviour, never that it really hit a paid API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.video.plugins.contracts import (
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoProviderPlugin,
    VideoRequest,
)
from app.video.plugins.errors import SandboxError
from app.video.plugins.sandbox import Sandbox

logger = get_logger("app.video.plugins.conformance")


# --------------------------------------------------------------------------- #
# Case + report types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The outcome of one conformance case."""

    name: str
    passed: bool
    required: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """The aggregate result of running the conformance contract against a plugin."""

    plugin_ref: str
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        """True when every *required* case passed (optional failures are tolerated)."""
        return all(r.passed for r in self.results if r.required)

    @property
    def failures(self) -> tuple[str, ...]:
        """Names of the required cases that failed."""
        return tuple(r.name for r in self.results if r.required and not r.passed)


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    """One named check the plugin must satisfy.

    ``check`` receives the live plugin, its declared profile, and a
    :class:`~app.video.plugins.sandbox.Sandbox` to drive calls through; it
    returns ``(passed, detail)``. A required case that fails quarantines the
    plugin; an optional case that fails is recorded but tolerated.
    """

    name: str
    required: bool
    check: Callable[
        [VideoProviderPlugin, CapabilityProfile, Sandbox],
        Awaitable[tuple[bool, str]],
    ]


# --------------------------------------------------------------------------- #
# The default LOCAL conformance contract
# --------------------------------------------------------------------------- #


async def _case_capabilities_match(
    plugin: VideoProviderPlugin, profile: CapabilityProfile, _sandbox: Sandbox
) -> tuple[bool, str]:
    """The runtime ``capabilities`` must equal the manifest-declared profile."""
    runtime = getattr(plugin, "capabilities", None)
    if runtime != profile:
        return False, "runtime capabilities differ from the declared manifest profile"
    return True, ""


async def _case_profile_self_consistent(
    _plugin: VideoProviderPlugin, profile: CapabilityProfile, _sandbox: Sandbox
) -> tuple[bool, str]:
    """The profile must be internally coherent (bounds + r2v invariant)."""
    if profile.min_duration_s > profile.max_duration_s:
        return False, "min_duration_s exceeds max_duration_s"
    if profile.min_duration_s <= 0:
        return False, "min_duration_s must be positive"
    r2v = RenderMode.REFERENCE_TO_VIDEO in profile.modes
    if r2v and profile.max_reference_images <= 0:
        return False, "reference_to_video advertised but max_reference_images is 0"
    return True, ""


async def _case_probe_no_spend(
    plugin: VideoProviderPlugin, _profile: CapabilityProfile, sandbox: Sandbox
) -> tuple[bool, str]:
    """``probe`` must return a :class:`ProbeResult` under the sandbox guards."""
    try:
        call = await sandbox.probe(plugin)
    except SandboxError as exc:
        return False, f"probe violated the sandbox: {exc.code}"
    if not isinstance(call.value, ProbeResult):
        return False, "probe did not return a ProbeResult"
    return True, ""


async def _case_generate_honours_request(
    plugin: VideoProviderPlugin, profile: CapabilityProfile, sandbox: Sandbox
) -> tuple[bool, str]:
    """For each advertised mode, ``generate`` returns an artifact for that mode.

    Runs under the sandbox with no-spend host services; checks that the returned
    :class:`VideoArtifact` echoes the requested mode and a positive duration —
    the contract every downstream router relies on. A sandbox violation here is
    a conformance failure (the gate cannot be bypassed by escaping the sandbox).
    """
    for mode in sorted(profile.modes, key=lambda m: m.value):
        request = _request_for(mode, profile)
        try:
            call = await sandbox.generate(plugin, request)
        except SandboxError as exc:
            return False, f"generate({mode.value}) violated the sandbox: {exc.code}"
        artifact = call.value
        if not isinstance(artifact, VideoArtifact):
            return False, f"generate({mode.value}) did not return a VideoArtifact"
        if artifact.mode != mode:
            return False, f"generate({mode.value}) returned mode {artifact.mode.value}"
        if artifact.duration_s <= 0 or not artifact.clip_url:
            return False, f"generate({mode.value}) returned an empty/zero-length artifact"
    return True, ""


def _request_for(mode: RenderMode, profile: CapabilityProfile) -> VideoRequest:
    """A minimal, valid request for ``mode`` (conditioning URLs are dummies)."""
    duration = max(profile.min_duration_s, min(5.0, profile.max_duration_s))
    base: dict[str, object] = {
        "mode": mode,
        "prompt": "conformance probe scene",
        "duration_s": duration,
        "resolution": next(iter(sorted(profile.resolutions)), "720P"),
        "request_id": "conformance",
    }
    if mode in (RenderMode.IMAGE_TO_VIDEO, RenderMode.VIDEO_CONTINUATION):
        base["image_url"] = "https://example.invalid/frame.png"
    if mode is RenderMode.VIDEO_CONTINUATION:
        base["source_video_url"] = "https://example.invalid/prior.mp4"
    if mode is RenderMode.FIRST_LAST_FRAME:
        base["first_frame_url"] = "https://example.invalid/a.png"
        base["last_frame_url"] = "https://example.invalid/b.png"
    if mode is RenderMode.REFERENCE_TO_VIDEO:
        base["reference_image_urls"] = ["https://example.invalid/ref.png"]
    if mode is RenderMode.INSTRUCTION_EDIT:
        base["source_video_url"] = "https://example.invalid/src.mp4"
    return VideoRequest(**base)


#: The default conformance contract. ``capabilities_match`` /
#: ``profile_self_consistent`` / ``probe_no_spend`` / ``generate_honours_request``
#: are required; an integration can append optional cases.
DEFAULT_CONTRACT: tuple[ConformanceCase, ...] = (
    ConformanceCase("capabilities_match", True, _case_capabilities_match),
    ConformanceCase("profile_self_consistent", True, _case_profile_self_consistent),
    ConformanceCase("probe_no_spend", True, _case_probe_no_spend),
    ConformanceCase("generate_honours_request", True, _case_generate_honours_request),
)


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ConformanceHarness:
    """Runs a conformance contract against a plugin, producing a report."""

    contract: tuple[ConformanceCase, ...] = DEFAULT_CONTRACT
    #: Extra integration-supplied cases appended to the default contract.
    extra: tuple[ConformanceCase, ...] = field(default_factory=tuple)

    async def run(
        self,
        *,
        plugin_ref: str,
        plugin: VideoProviderPlugin,
        profile: CapabilityProfile,
        sandbox: Sandbox,
    ) -> ConformanceReport:
        """Run every case; one case raising is itself a failure, never a crash."""
        results: list[CaseResult] = []
        for case in (*self.contract, *self.extra):
            try:
                passed, detail = await case.check(plugin, profile, sandbox)
            except Exception as exc:  # noqa: BLE001 - a throwing case is a failed case
                passed, detail = False, f"case raised {type(exc).__name__}: {exc}"
            results.append(
                CaseResult(name=case.name, passed=passed, required=case.required, detail=detail)
            )
        report = ConformanceReport(plugin_ref=plugin_ref, results=tuple(results))
        logger.info(
            "conformance_run",
            plugin=plugin_ref,
            passed=report.passed,
            failures=list(report.failures),
        )
        return report


__all__ = [
    "DEFAULT_CONTRACT",
    "CaseResult",
    "ConformanceCase",
    "ConformanceHarness",
    "ConformanceReport",
]
