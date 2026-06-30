"""The provider-onboarding wizard: a reversible, explainable state machine.

Bringing a new video model into the marketplace is a sequence of *gates*, each
of which must pass before the listing advances. The wizard models that as an
explicit state machine over :class:`OnboardingStage`:

    DECLARED → MANIFEST_VALID → CAPABILITIES_VALID → CONFORMANCE_PASSED
             → CONFIGURED → STAGED(preview) → ACTIVATED(active)

Every transition is:

* **explainable** — a :class:`GateResult` records the gate name, pass/fail, and a
  list of human ``reasons`` (why it failed and how to fix). On success the
  reasons explain what was verified.
* **reversible** — :meth:`OnboardingWizard.revert_to` rolls back to any earlier
  stage, discarding the forward history beyond it. Activation is reversible too
  (``deactivate`` / via the lifecycle manager).

The conformance gate is a **local protocol dry-run**: it never calls a provider.
It exercises the listing's declared contract against a deterministic in-process
:class:`ConformanceProbe` (a callable the host supplies, defaulting to a probe
that simply re-derives the declared capabilities). This keeps the wizard fully
offline and test-deterministic — no network, no credits, no ``KINORA_LIVE_VIDEO``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.video.marketplace.errors import GateFailedError, InvalidTransitionError
from app.video.marketplace.listing import ModelListing, validate_listing
from app.video.marketplace.types import (
    Capability,
    ListingStatus,
    Modality,
)


class OnboardingStage(StrEnum):
    """The ordered stages a listing passes through during onboarding."""

    DECLARED = "declared"
    MANIFEST_VALID = "manifest_valid"
    CAPABILITIES_VALID = "capabilities_valid"
    CONFORMANCE_PASSED = "conformance_passed"
    CONFIGURED = "configured"
    STAGED = "staged"  # listing.status == PREVIEW
    ACTIVATED = "activated"  # listing.status == ACTIVE

    @property
    def ordinal(self) -> int:
        return _STAGE_ORDER[self]


_STAGE_SEQUENCE: tuple[OnboardingStage, ...] = (
    OnboardingStage.DECLARED,
    OnboardingStage.MANIFEST_VALID,
    OnboardingStage.CAPABILITIES_VALID,
    OnboardingStage.CONFORMANCE_PASSED,
    OnboardingStage.CONFIGURED,
    OnboardingStage.STAGED,
    OnboardingStage.ACTIVATED,
)
_STAGE_ORDER: dict[OnboardingStage, int] = {s: i for i, s in enumerate(_STAGE_SEQUENCE)}


@dataclass(frozen=True)
class GateResult:
    """The explainable outcome of one gate evaluation."""

    gate: str
    passed: bool
    reasons: tuple[str, ...] = ()
    #: The stage the wizard moved *to* (only meaningful when ``passed``).
    to_stage: OnboardingStage | None = None


@dataclass(frozen=True)
class ConformanceReport:
    """The result of the local protocol dry-run conformance probe."""

    ok: bool
    checked_modalities: tuple[Modality, ...]
    checked_capabilities: tuple[Capability, ...]
    findings: tuple[str, ...] = ()


#: A conformance probe takes a listing and returns a report. The default probe
#: is deterministic and offline; a host may inject a richer (still offline) one.
ConformanceProbe = Callable[[ModelListing], ConformanceReport]


def default_conformance_probe(listing: ModelListing) -> ConformanceReport:
    """A deterministic, offline protocol dry-run.

    It verifies the *internal* contract of the declared listing rather than
    calling any provider: every advertised capability is recognized, every
    video-output modality has a positive duration ceiling, and audio capability
    is coherent. Findings explain anything that looks off. This stands in for a
    real wire-protocol smoke test while remaining free and reproducible.
    """
    findings: list[str] = []

    if listing.max_duration_s <= 0:
        findings.append("max_duration_s must be positive for a renderable model")

    if Capability.LONG_DURATION in listing.capabilities and listing.max_duration_s < 6.0:
        findings.append(
            "LONG_DURATION advertised but max_duration_s < 6s; ceiling looks inconsistent"
        )

    if Capability.AUDIO_TRACK in listing.capabilities and not any(
        m.is_video_output for m in listing.modalities
    ):
        findings.append("AUDIO_TRACK requires a video-output modality")

    # an image-to-video / reference-to-video model that cannot accept conditioning
    # is suspicious; we just note it (not fatal).
    conditioning = {
        Modality.IMAGE_TO_VIDEO,
        Modality.REFERENCE_TO_VIDEO,
        Modality.KEYFRAME_INTERPOLATION,
    }
    if (
        any(m in conditioning for m in listing.modalities)
        and Capability.FIRST_LAST_FRAME not in listing.capabilities
        and Capability.CHARACTER_CONSISTENCY not in listing.capabilities
    ):
        findings.append(
            "conditioning modality declared without FIRST_LAST_FRAME/CHARACTER_CONSISTENCY "
            "(advisory)"
        )

    ok = not any(f for f in findings if "advisory" not in f)
    return ConformanceReport(
        ok=ok,
        checked_modalities=listing.modalities,
        checked_capabilities=listing.capabilities,
        findings=tuple(findings),
    )


@dataclass
class OnboardingWizard:
    """Drives one listing from DECLARED to ACTIVATED through explainable gates.

    The wizard owns a mutable *working* listing (rebuilt via
    :meth:`ModelListing.evolve` on each transition so each snapshot stays frozen
    and re-validated) plus a stage cursor and an append-only ``history`` of
    :class:`GateResult`. ``revert_to`` makes the whole flow reversible.
    """

    listing: ModelListing
    probe: ConformanceProbe = default_conformance_probe
    stage: OnboardingStage = OnboardingStage.DECLARED
    history: list[GateResult] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def declare(
        cls, listing: ModelListing, *, probe: ConformanceProbe | None = None
    ) -> OnboardingWizard:
        """Begin onboarding for ``listing`` (forced to DRAFT status at declaration)."""
        draft = (
            listing
            if listing.status == ListingStatus.DRAFT
            else listing.evolve(status=ListingStatus.DRAFT)
        )
        wiz = cls(listing=draft, probe=probe or default_conformance_probe)
        wiz._record(
            GateResult(
                gate="declare",
                passed=True,
                reasons=(f"declared {draft.key} for onboarding",),
                to_stage=OnboardingStage.DECLARED,
            )
        )
        return wiz

    # ------------------------------------------------------------------ #
    # gates (each advances exactly one stage)
    # ------------------------------------------------------------------ #
    def validate_manifest(self) -> GateResult:
        """Gate 1: the listing manifest is structurally valid."""
        self._require_stage(OnboardingStage.DECLARED, gate="validate_manifest")
        try:
            validate_listing(self.listing)
        except Exception as exc:  # ListingValidationError
            return self._fail(
                "validate_manifest",
                [f"manifest invalid: {exc}", "fix the offending fields and re-run"],
            )
        return self._advance(
            "validate_manifest",
            OnboardingStage.MANIFEST_VALID,
            [
                "manifest parsed and structurally valid",
                f"identity {self.listing.provider}/{self.listing.model_id}@{self.listing.version}",
            ],
        )

    def validate_capabilities(self) -> GateResult:
        """Gate 2: the listing advertises a coherent, non-empty capability set."""
        self._require_stage(OnboardingStage.MANIFEST_VALID, gate="validate_capabilities")
        reasons: list[str] = []
        if not self.listing.modalities:
            reasons.append("no modalities declared")
        # every advertised capability must be a known enum member (guaranteed by
        # construction, but we make the gate explicit & explainable).
        unknown = [c for c in self.listing.capabilities if not isinstance(c, Capability)]
        if unknown:  # pragma: no cover - construction prevents this
            reasons.append(f"unknown capabilities: {unknown}")
        if reasons:
            return self._fail("validate_capabilities", reasons)
        return self._advance(
            "validate_capabilities",
            OnboardingStage.CAPABILITIES_VALID,
            [
                f"{len(self.listing.modalities)} modality(ies), "
                f"{len(self.listing.capabilities)} capability(ies) recognized",
            ],
        )

    def run_conformance(self) -> GateResult:
        """Gate 3: local protocol dry-run (offline; never calls a provider)."""
        self._require_stage(OnboardingStage.CAPABILITIES_VALID, gate="run_conformance")
        report = self.probe(self.listing)
        if not report.ok:
            fatal = [f for f in report.findings if "advisory" not in f]
            return self._fail(
                "run_conformance",
                ["conformance dry-run failed", *fatal],
            )
        reasons = [
            "local protocol dry-run passed (no provider call)",
            f"verified {len(report.checked_capabilities)} capability(ies)",
        ]
        reasons.extend(report.findings)  # advisories surfaced but non-blocking
        return self._advance("run_conformance", OnboardingStage.CONFORMANCE_PASSED, reasons)

    def configure(
        self,
        *,
        require_pricing: bool = True,
        require_region: bool = True,
        require_commercial_license: bool = False,
    ) -> GateResult:
        """Gate 4: price/region (and optionally license) configuration is complete."""
        self._require_stage(OnboardingStage.CONFORMANCE_PASSED, gate="configure")
        reasons: list[str] = []
        if require_pricing and not self.listing.pricing:
            reasons.append("no pricing tier configured; add at least one PricingTier")
        if require_region and not self.listing.region.regions:  # pragma: no cover - validated
            reasons.append("no serving region configured")
        if require_commercial_license and not self.listing.license_class.commercial_safe:
            reasons.append(
                f"license_class={self.listing.license_class.value} is not commercial-safe"
            )
        if not self.listing.tos_accepted:
            reasons.append("provider ToS not accepted (set tos_accepted=True)")
        if reasons:
            return self._fail("configure", reasons)
        return self._advance(
            "configure",
            OnboardingStage.CONFIGURED,
            [
                f"{len(self.listing.pricing)} pricing tier(s) configured",
                f"regions {[r.value for r in self.listing.region.regions]}",
                "ToS accepted",
            ],
        )

    def stage_preview(self, *, now: datetime | None = None) -> GateResult:
        """Gate 5: stage the listing as PREVIEW (selectable, clearly pre-GA)."""
        self._require_stage(OnboardingStage.CONFIGURED, gate="stage_preview")
        self.listing = self.listing.evolve(status=ListingStatus.PREVIEW, now=now)
        return self._advance(
            "stage_preview",
            OnboardingStage.STAGED,
            ["listing staged as PREVIEW; visible & selectable for opt-in renders"],
        )

    def activate(self, *, now: datetime | None = None) -> GateResult:
        """Gate 6: promote PREVIEW → ACTIVE (full marketplace availability)."""
        self._require_stage(OnboardingStage.STAGED, gate="activate")
        try:
            self.listing = self.listing.evolve(status=ListingStatus.ACTIVE, now=now)
        except Exception as exc:
            # ACTIVE has extra invariants (pricing + ToS); surface them, stay STAGED.
            return self._fail("activate", [f"cannot activate: {exc}"])
        return self._advance(
            "activate",
            OnboardingStage.ACTIVATED,
            ["listing ACTIVE; available across the marketplace"],
        )

    def run_all(
        self,
        *,
        now: datetime | None = None,
        require_pricing: bool = True,
        require_region: bool = True,
        require_commercial_license: bool = False,
    ) -> list[GateResult]:
        """Run every gate in order, stopping at the first failure.

        Convenience for the happy path / tests. Returns the gate results
        produced (the last one indicates where it stopped).
        """
        results: list[GateResult] = []
        steps: list[Callable[[], GateResult]] = [
            self.validate_manifest,
            self.validate_capabilities,
            self.run_conformance,
            lambda: self.configure(
                require_pricing=require_pricing,
                require_region=require_region,
                require_commercial_license=require_commercial_license,
            ),
            lambda: self.stage_preview(now=now),
            lambda: self.activate(now=now),
        ]
        for step in steps:
            res = step()
            results.append(res)
            if not res.passed:
                break
        return results

    # ------------------------------------------------------------------ #
    # reversibility
    # ------------------------------------------------------------------ #
    def revert_to(self, stage: OnboardingStage, *, now: datetime | None = None) -> GateResult:
        """Roll back to an earlier ``stage``, discarding later progress.

        Adjusts the listing status to match the target stage (STAGED→PREVIEW,
        anything earlier→DRAFT, ACTIVATED stays ACTIVE) and appends an
        explainable revert record. Raises :class:`InvalidTransitionError` if the
        target is not strictly earlier than the current stage.
        """
        if stage.ordinal >= self.stage.ordinal:
            raise InvalidTransitionError(
                f"revert target {stage.value!r} is not earlier than current {self.stage.value!r}"
            )
        # map stage -> listing status
        if stage == OnboardingStage.STAGED:
            target_status = ListingStatus.PREVIEW
        else:
            target_status = ListingStatus.DRAFT
        if self.listing.status != target_status:
            self.listing = self.listing.evolve(status=target_status, now=now)
        self.stage = stage
        result = GateResult(
            gate="revert",
            passed=True,
            reasons=(f"reverted to {stage.value}; status now {target_status.value}",),
            to_stage=stage,
        )
        self._record(result)
        return result

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _require_stage(self, expected: OnboardingStage, *, gate: str) -> None:
        if self.stage != expected:
            raise InvalidTransitionError(
                f"gate {gate!r} requires stage {expected.value!r}, but wizard is at "
                f"{self.stage.value!r}"
            )

    def _advance(
        self, gate: str, to_stage: OnboardingStage, reasons: list[str]
    ) -> GateResult:
        self.stage = to_stage
        result = GateResult(gate=gate, passed=True, reasons=tuple(reasons), to_stage=to_stage)
        self._record(result)
        return result

    def _fail(self, gate: str, reasons: list[str]) -> GateResult:
        result = GateResult(gate=gate, passed=False, reasons=tuple(reasons), to_stage=None)
        self._record(result)
        return result

    def _record(self, result: GateResult) -> None:
        self.history.append(result)

    def require_passed(self, result: GateResult) -> GateResult:
        """Raise :class:`GateFailedError` if ``result`` failed (for strict callers)."""
        if not result.passed:
            raise GateFailedError(
                f"gate {result.gate!r} failed", reasons=list(result.reasons)
            )
        return result


def _utcnow() -> datetime:  # pragma: no cover - trivial
    return datetime.now(UTC)


__all__ = [
    "ConformanceProbe",
    "ConformanceReport",
    "GateResult",
    "OnboardingStage",
    "OnboardingWizard",
    "default_conformance_probe",
]
