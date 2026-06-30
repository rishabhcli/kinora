"""Immutable, typed value objects threaded through the safety gateway.

Pure data — no I/O, no model calls. The flow is:

    classifier  -> Finding(s)                  (a category at a severity)
                -> rule engine / policy        -> PromptAssessment / OutputAssessment
                -> softener (prompt only)       -> SofteningResult
                -> gateway                      -> PromptDecision / OutputVerdict
                -> routing                      -> RoutingPlan
                -> decision log                 -> DecisionRecord (hash-chained)

Pydantic v2 models, frozen where they are values, so they serialise cleanly to
the API and to JSONB audit payloads and validate on construction. Every verdict
carries the findings that drove it so it is **explainable** end-to-end.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.safety.taxonomy import SafetyAction, SafetyCategory, Severity


class SafetySurface(enum.StrEnum):
    """Which point in the generation pipeline produced the content being checked."""

    PROMPT = "prompt"  # a render prompt about to be sent to a video/image provider
    NEGATIVE_PROMPT = "negative_prompt"  # the negative prompt (rarely blocks)
    KEYFRAME = "keyframe"  # a generated still (image-gen lane)
    CLIP = "clip"  # a generated video clip (sampled frames)
    BOOK = "book"  # whole-book text, for the advisory tagger


class Finding(BaseModel):
    """One classifier/rule finding: a category at a confidence + bucketed severity.

    ``score`` is the raw 0..1 signal; ``severity`` is the tier the policy compares
    against. Keeping both lets the decision log record the raw signal while the
    action is decided on the tier. ``source`` says whether a rule or a model fired
    it (for the audit trail and for tests that assert determinism).
    """

    model_config = ConfigDict(frozen=True)

    category: SafetyCategory
    score: float = Field(ge=0.0, le=1.0)
    severity: Severity
    #: The matched term / span / frame index — what to point at in the UI.
    rationale: str | None = None
    #: "rule" (deterministic engine) or "model" (classifier seam) or "fake".
    source: str = "rule"

    @classmethod
    def of(
        cls,
        category: SafetyCategory,
        score: float,
        *,
        rationale: str | None = None,
        source: str = "rule",
    ) -> Finding:
        """Build a finding, bucketing ``score`` into a :class:`Severity` tier."""
        return cls(
            category=category,
            score=score,
            severity=Severity.from_score(score),
            rationale=rationale,
            source=source,
        )

    @property
    def positive(self) -> bool:
        """True for a real (non-SAFE, severity > NONE) finding."""
        return self.category is not SafetyCategory.SAFE and self.severity > Severity.NONE


def _worst_severity(findings: list[Finding]) -> Severity:
    worst = Severity.NONE
    for f in findings:
        if f.positive and f.severity > worst:
            worst = f.severity
    return worst


def _positive(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.positive]


def _ordered_categories(findings: list[Finding]) -> list[SafetyCategory]:
    seen: dict[SafetyCategory, None] = {}
    for f in sorted(findings, key=lambda x: x.severity, reverse=True):
        if f.positive:
            seen.setdefault(f.category, None)
    return list(seen)


class SofteningResult(BaseModel):
    """The outcome of an intent-preserving auto-soften pass over a prompt.

    ``changed`` is False when no transform was needed/possible; ``softened_prompt``
    then equals the original. ``transforms`` lists each rewrite applied (so the
    decision is explainable and the diff is auditable), and ``unsoftenable`` lists
    any categories the softener was *not* allowed to touch (e.g. hate), which the
    gateway then escalates rather than silently passing through.
    """

    model_config = ConfigDict(frozen=True)

    changed: bool
    original_prompt: str
    softened_prompt: str
    #: Human/machine-readable list of the rewrites applied, e.g.
    #: "violence: 'graphic stabbing' -> 'a tense confrontation, implied off-frame'".
    transforms: list[str] = Field(default_factory=list)
    #: Categories present that the softener may not rewrite (escalated by the gate).
    unsoftenable: list[SafetyCategory] = Field(default_factory=list)
    #: Categories the softener neutralised so they no longer drive a block.
    resolved: list[SafetyCategory] = Field(default_factory=list)

    @property
    def intent_preserved(self) -> bool:
        """A soften is intent-preserving when it left non-empty, non-trivial text.

        The softener never empties a prompt; an all-redaction would be a block, not
        a transform. This guards the invariant the tests assert.
        """
        return bool(self.softened_prompt.strip())


class PromptAssessment(BaseModel):
    """The classifier + rule-engine read of one prompt, before any decision.

    A read-model: just the findings and their worst severity. The gateway turns
    this (+ softening + the provider profile) into a :class:`PromptDecision`.
    """

    model_config = ConfigDict(frozen=True)

    surface: SafetySurface
    findings: list[Finding]
    classifier: str = "rule"
    degraded: bool = False

    @property
    def max_severity(self) -> Severity:
        return _worst_severity(self.findings)

    def positive_findings(self) -> list[Finding]:
        return _positive(self.findings)

    @property
    def categories(self) -> list[SafetyCategory]:
        return _ordered_categories(self.findings)


class PromptDecision(BaseModel):
    """The gateway's resolved, explainable decision for one render prompt.

    Carries the *effective* prompt the pipeline should send (the softened text when
    ``action`` is ``TRANSFORM``, else the original), the findings that drove the
    action, the softening provenance, and the routing plan computed from the
    per-provider profiles.
    """

    model_config = ConfigDict(frozen=True)

    surface: SafetySurface
    action: SafetyAction
    severity: Severity
    driving_findings: list[Finding]
    #: The text the pipeline should actually use (softened on TRANSFORM).
    effective_prompt: str
    softening: SofteningResult | None = None
    routing: RoutingPlan | None = None
    policy_version: str = "default"
    classifier: str = "rule"
    degraded: bool = False
    reason: str = ""

    @property
    def allowed(self) -> bool:
        """True when the pipeline may proceed (ALLOW or TRANSFORM)."""
        return self.action in (SafetyAction.ALLOW, SafetyAction.TRANSFORM)

    @property
    def blocked(self) -> bool:
        return self.action is SafetyAction.BLOCK

    @property
    def quarantined(self) -> bool:
        return self.action is SafetyAction.QUARANTINE

    @property
    def transformed(self) -> bool:
        return self.action is SafetyAction.TRANSFORM

    @property
    def categories(self) -> list[SafetyCategory]:
        return _ordered_categories(self.driving_findings)


class OutputVerdict(enum.StrEnum):
    """The post-generation coarse outcome (API-facing)."""

    ALLOW = "allow"  # the clip/keyframe may be shown
    QUARANTINE = "quarantine"  # held — not shown, pending review
    BLOCK = "block"  # destroyed — never shown (zero-tolerance hit on output)


class OutputAssessment(BaseModel):
    """The post-generation screen of a generated keyframe / clip.

    Built from sampled-frame classification. ``verdict`` is the allow/quarantine/
    block call; ``driving_findings`` explain it; ``sampled_frames`` records how many
    frames were inspected (for the audit trail and degradation telemetry).
    """

    model_config = ConfigDict(frozen=True)

    surface: SafetySurface
    verdict: OutputVerdict
    severity: Severity
    driving_findings: list[Finding]
    sampled_frames: int = 0
    classifier: str = "rule"
    policy_version: str = "default"
    degraded: bool = False
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is OutputVerdict.ALLOW

    @property
    def categories(self) -> list[SafetyCategory]:
        return _ordered_categories(self.driving_findings)


class ProviderRanking(BaseModel):
    """One provider's standing for a given prompt assessment.

    ``viable`` is False when the provider's policy profile would reject the
    content; ``refusing_categories`` say which categories tripped it. ``score`` is
    a comparable suitability number (higher = better) the router sorts on.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    viable: bool
    score: float
    refusing_categories: list[SafetyCategory] = Field(default_factory=list)
    note: str = ""


class RoutingPlan(BaseModel):
    """Which providers to try (and which to avoid) for a softened prompt.

    The router uses ``ordered_providers`` (best-first) to skip a provider that
    would certainly reject the content, saving a wasted metered render. When
    ``ordered_providers`` is empty, *every* known provider refuses the content and
    the gateway should not attempt a live render at all.
    """

    model_config = ConfigDict(frozen=True)

    #: Viable providers, best-first.
    ordered_providers: list[str]
    #: Per-provider rankings (viable + non-viable) for explainability.
    rankings: list[ProviderRanking] = Field(default_factory=list)
    #: Categories that caused at least one provider to be dropped.
    avoided_categories: list[SafetyCategory] = Field(default_factory=list)
    reason: str = ""

    @property
    def has_viable_provider(self) -> bool:
        return bool(self.ordered_providers)

    @property
    def best_provider(self) -> str | None:
        return self.ordered_providers[0] if self.ordered_providers else None


class SafetyContext(BaseModel):
    """Who/what a check is *about*, for attribution + the decision log.

    Threaded through every gateway call so the immutable decision log can attribute
    a decision to a tenant / book / shot / render job.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = "default"
    user_id: str | None = None
    book_id: str | None = None
    shot_id: str | None = None
    session_id: str | None = None
    #: Free-form correlation id (e.g. the render job id) for cross-referencing.
    correlation_id: str | None = None


class AgeRating(enum.StrEnum):
    """A coarse, MPAA/PEGI-flavoured age band for a whole book's adaptation."""

    G = "G"  # general audiences
    PG = "PG"  # parental guidance
    PG13 = "PG-13"  # some material may be inappropriate < 13
    R = "R"  # restricted — strong content
    NC17 = "NC-17"  # adults only

    @property
    def rank(self) -> int:
        return {
            AgeRating.G: 0,
            AgeRating.PG: 1,
            AgeRating.PG13: 2,
            AgeRating.R: 3,
            AgeRating.NC17: 4,
        }[self]

    @classmethod
    def strictest(cls, ratings: list[AgeRating]) -> AgeRating:
        return max(ratings, default=cls.G, key=lambda r: r.rank)


class ContentAdvisory(BaseModel):
    """The age-rating + content-advisory descriptors for a book's adaptation.

    ``descriptors`` are the human-facing reasons (e.g. "violence", "brief
    nudity"), ``rating`` the strictest band any chapter reached, and
    ``category_severity`` the worst severity observed per category (so the UI can
    render a detailed advisory like a film classification card).
    """

    model_config = ConfigDict(frozen=True)

    rating: AgeRating
    descriptors: list[str] = Field(default_factory=list)
    category_severity: dict[SafetyCategory, Severity] = Field(default_factory=dict)
    rationale: str = ""


class DecisionKind(enum.StrEnum):
    """What a decision-log record is about (one of the gateway's outputs)."""

    PROMPT = "prompt"
    OUTPUT = "output"
    OVERRIDE = "override"
    APPEAL = "appeal"
    ADVISORY = "advisory"


class AppealState(enum.StrEnum):
    """The lifecycle of an appeal/override against a recorded decision."""

    NONE = "none"
    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"


class DecisionRecordView(BaseModel):
    """A read-model projection of one immutable decision-log record (API-facing)."""

    model_config = ConfigDict(frozen=True)

    seq: int
    id: str
    kind: DecisionKind
    tenant_id: str
    surface: SafetySurface | None
    action: str
    severity: Severity
    categories: list[SafetyCategory]
    reason: str
    book_id: str | None
    shot_id: str | None
    appeal_state: AppealState
    prev_hash: str
    this_hash: str
    created_at: datetime
    payload: dict[str, Any] | None = None


__all__ = [
    "AgeRating",
    "AppealState",
    "ContentAdvisory",
    "DecisionKind",
    "DecisionRecordView",
    "Finding",
    "OutputAssessment",
    "OutputVerdict",
    "PromptAssessment",
    "PromptDecision",
    "ProviderRanking",
    "RoutingPlan",
    "SafetyAction",
    "SafetyCategory",
    "SafetyContext",
    "SafetySurface",
    "Severity",
    "SofteningResult",
]
