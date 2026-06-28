"""Immutable value objects shared across the moderation layers (§10).

These are pure data — no I/O, no model calls. They flow:

    classifier  -> ClassificationResult (a bag of ContentLabels)
                -> policy engine
                -> ModerationVerdict (a Decision + the labels that drove it)
                -> gate / review queue / audit log

Pydantic models (frozen where they are values) so they serialise cleanly to the
API and to the JSONB audit payloads, and validate on construction.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.moderation.taxonomy import Disposition, ModerationCategory, Severity


class Surface(enum.StrEnum):
    """Which trust boundary produced the content being screened."""

    INGEST_TEXT = "ingest_text"  # source book text at import
    INGEST_PAGE = "ingest_page"  # a rendered source page image at import
    KEYFRAME = "keyframe"  # a generated still (image-gen lane)
    CLIP = "clip"  # a generated video clip (Wan lane)
    COMMENT = "comment"  # a director region comment / free-text input
    NARRATION = "narration"  # synthesized narration text


class ContentLabel(BaseModel):
    """One classifier finding: a category at a confidence/severity score.

    ``score`` is the classifier's raw 0..1 confidence; ``severity`` is the bucketed
    tier the policy engine actually compares against. Keeping both lets the audit
    log record the model's raw signal while the decision is made on the tier.
    """

    model_config = ConfigDict(frozen=True)

    category: ModerationCategory
    score: float = Field(ge=0.0, le=1.0)
    severity: Severity
    #: Optional human/machine note: the span, the matched term, the frame index.
    rationale: str | None = None

    @classmethod
    def of(
        cls,
        category: ModerationCategory,
        score: float,
        *,
        rationale: str | None = None,
    ) -> ContentLabel:
        """Build a label, bucketing ``score`` into a :class:`Severity` tier."""
        return cls(
            category=category,
            score=score,
            severity=Severity.from_score(score),
            rationale=rationale,
        )


class ClassificationResult(BaseModel):
    """The full output of one classifier pass over one piece of content.

    A classifier always returns at least one label; when nothing fired it returns
    a single :data:`ModerationCategory.SAFE` label so downstream code never has to
    special-case the empty list.
    """

    model_config = ConfigDict(frozen=True)

    surface: Surface
    labels: list[ContentLabel]
    #: The classifier implementation that produced this (for the audit trail).
    classifier: str = "unknown"
    #: True when the classifier could not run (model error / unsupported media)
    #: and the result is a conservative fallback rather than a real judgment.
    degraded: bool = False

    @property
    def max_severity(self) -> Severity:
        """The worst severity across all labels (NONE if only SAFE)."""
        worst = Severity.NONE
        for lab in self.labels:
            if lab.category is not ModerationCategory.SAFE and lab.severity > worst:
                worst = lab.severity
        return worst

    def positive_labels(self) -> list[ContentLabel]:
        """Labels that are not SAFE and fired above NONE severity."""
        return [
            lab
            for lab in self.labels
            if lab.category is not ModerationCategory.SAFE and lab.severity > Severity.NONE
        ]


class ModerationVerdict(BaseModel):
    """The policy engine's decision for one classification, with provenance.

    ``decision`` is the resolved disposition; ``driving_labels`` are exactly the
    labels that pushed it there (so the UI/audit can say *why*); ``policy_version``
    pins which tenant policy produced it.
    """

    model_config = ConfigDict(frozen=True)

    surface: Surface
    decision: Disposition
    severity: Severity
    driving_labels: list[ContentLabel]
    classifier: str = "unknown"
    policy_version: str = "default"
    degraded: bool = False
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Disposition.ALLOW

    @property
    def blocked(self) -> bool:
        return self.decision is Disposition.BLOCK

    @property
    def flagged(self) -> bool:
        return self.decision is Disposition.FLAG

    @property
    def categories(self) -> list[ModerationCategory]:
        """The distinct categories that drove the decision, worst-first."""
        seen: dict[ModerationCategory, None] = {}
        for lab in sorted(self.driving_labels, key=lambda x: x.severity, reverse=True):
            seen.setdefault(lab.category, None)
        return list(seen)


class Decision(enum.StrEnum):
    """A coarse outcome alias used at the gate boundary (API-facing)."""

    PASS = "pass"  # content may proceed (ALLOW, or FLAG that the gate lets through)
    HOLD = "hold"  # content is held pending review (FLAG on a blocking gate)
    REJECT = "reject"  # content is refused (BLOCK)


class ModerationContext(BaseModel):
    """Who/what a screening is *about*, for ownership + escalation tracking.

    Threaded through every gate call so the audit log, review queue, and
    repeat-offender tracker can attribute a violation to a tenant/user/book.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = "default"
    user_id: str | None = None
    book_id: str | None = None
    shot_id: str | None = None
    session_id: str | None = None
    #: Free-form correlation id (e.g. the render job id) for cross-referencing.
    correlation_id: str | None = None


class ReviewState(enum.StrEnum):
    """The human-review / takedown / appeal state machine (see :mod:`.review`)."""

    PENDING = "pending"  # flagged, awaiting a reviewer
    UNDER_REVIEW = "under_review"  # claimed by a reviewer
    APPROVED = "approved"  # reviewer cleared it — content allowed
    REJECTED = "rejected"  # reviewer upheld the block — content stays down
    TAKEDOWN = "takedown"  # actively removed (was live, now pulled)
    APPEALED = "appealed"  # the owner contests a rejection/takedown
    APPEAL_GRANTED = "appeal_granted"  # appeal succeeded — reinstated
    APPEAL_DENIED = "appeal_denied"  # appeal failed — stays down
    ESCALATED = "escalated"  # bumped to a senior reviewer / policy owner


#: Terminal states the review state machine never transitions out of.
TERMINAL_REVIEW_STATES: frozenset[ReviewState] = frozenset(
    {ReviewState.APPROVED, ReviewState.APPEAL_GRANTED, ReviewState.APPEAL_DENIED}
)


class ReviewItemView(BaseModel):
    """A read-model projection of one review-queue item (API-facing)."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    surface: Surface
    state: ReviewState
    decision: Disposition
    severity: Severity
    categories: list[ModerationCategory]
    book_id: str | None
    shot_id: str | None
    user_id: str | None
    reason: str
    assignee_id: str | None
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any] | None = None


__all__ = [
    "TERMINAL_REVIEW_STATES",
    "ClassificationResult",
    "ContentLabel",
    "Decision",
    "Disposition",
    "ModerationCategory",
    "ModerationContext",
    "ModerationVerdict",
    "ReviewItemView",
    "ReviewState",
    "Severity",
    "Surface",
]
