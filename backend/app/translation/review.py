"""The review / human post-edit workflow.

Machine translation is not the end of the line for content a reader sees: a
segment whose quality estimate fell below the bar, or whose markup/glossary
check warned, is flagged for a human to post-edit. This module owns the *state
machine* for that workflow and the service that drives it over the persistence
layer (:mod:`.artifacts`).

States (``ReviewStatus``):

    PENDING ──claim──▶ IN_REVIEW ──edit────▶ EDITED
       │                  │   └──approve───▶ APPROVED
       │                  └──reject────────▶ REJECTED
       └──approve/edit/reject (direct) ─────▶ …

The allowed transitions are enforced (`assert_transition`) so the API can't put a
review into an illegal state. When a reviewer EDITs a segment, the post-edited
text becomes the segment's translation with origin ``POST_EDIT`` and is written
back into the translation memory at quality 1.0 — so the human's correction is
reused for free on every later identical segment (the same §8.7 cache win that
makes a re-read free now also amortizes human effort).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger

from .artifacts import ReviewStatus, TranslationRepo, TranslationReview, TranslationSegment
from .errors import ReviewStateError
from .memory_store import MemoryEntry, TranslationMemory
from .types import ContentKind, TranslationOrigin

logger = get_logger("app.translation.review")

#: Legal transitions in the review state machine.
_ALLOWED: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.PENDING: frozenset(
        {ReviewStatus.IN_REVIEW, ReviewStatus.EDITED, ReviewStatus.APPROVED, ReviewStatus.REJECTED}
    ),
    ReviewStatus.IN_REVIEW: frozenset(
        {ReviewStatus.EDITED, ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.PENDING}
    ),
    # Terminal-ish states can still be re-opened to PENDING (e.g. a later
    # glossary change reopens an APPROVED segment).
    ReviewStatus.EDITED: frozenset({ReviewStatus.PENDING, ReviewStatus.APPROVED}),
    ReviewStatus.APPROVED: frozenset({ReviewStatus.PENDING}),
    ReviewStatus.REJECTED: frozenset({ReviewStatus.PENDING, ReviewStatus.IN_REVIEW}),
}


def assert_transition(current: ReviewStatus, target: ReviewStatus) -> None:
    """Raise :class:`ReviewStateError` if ``current → target`` is not allowed."""
    if target == current:
        return
    if target not in _ALLOWED.get(current, frozenset()):
        raise ReviewStateError(f"illegal review transition {current.value} → {target.value}")


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    """Aggregate review state for a book (for the API / a dashboard)."""

    pending: int = 0
    in_review: int = 0
    edited: int = 0
    approved: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.in_review + self.edited + self.approved + self.rejected

    @property
    def open(self) -> int:
        """Reviews still requiring attention."""
        return self.pending + self.in_review


class ReviewWorkflow:
    """Drives the post-edit workflow over the persistence layer.

    The repo is the unit-of-work-bound :class:`TranslationRepo`; an optional
    in-process TM is updated so an accepted human edit is immediately reusable.
    """

    def __init__(self, repo: TranslationRepo, *, memory: TranslationMemory | None = None) -> None:
        self._repo = repo
        self._memory = memory

    async def claim(self, review_id: str, *, reviewer_id: str) -> TranslationReview:
        """Move a PENDING review to IN_REVIEW, assigning a reviewer."""
        review = await self._require(review_id)
        assert_transition(review.status, ReviewStatus.IN_REVIEW)
        review.status = ReviewStatus.IN_REVIEW
        review.reviewer_id = reviewer_id
        await self._repo.session.flush()
        return review

    async def approve(self, review_id: str, *, reviewer_id: str | None = None) -> TranslationReview:
        """Accept the machine output as-is (no edit)."""
        review = await self._require(review_id)
        assert_transition(review.status, ReviewStatus.APPROVED)
        review.status = ReviewStatus.APPROVED
        if reviewer_id:
            review.reviewer_id = reviewer_id
        await self._clear_segment_flag(review.segment_row_id)
        await self._repo.session.flush()
        return review

    async def edit(
        self, review_id: str, *, edited_text: str, reviewer_id: str | None = None
    ) -> TranslationReview:
        """Replace the machine output with a human post-edit.

        The edited text becomes the segment's translation (origin ``POST_EDIT``,
        quality 1.0, review flag cleared) and is written into the TM so it is
        reused on every later identical segment.
        """
        if not edited_text.strip():
            raise ReviewStateError("edited text is empty")
        review = await self._require(review_id)
        assert_transition(review.status, ReviewStatus.EDITED)
        review.status = ReviewStatus.EDITED
        review.edited_text = edited_text
        if reviewer_id:
            review.reviewer_id = reviewer_id
        segment = await self._apply_edit_to_segment(review.segment_row_id, edited_text)
        await self._repo.session.flush()
        if segment is not None and self._memory is not None:
            self._memory.put(
                MemoryEntry(
                    source_text=segment.source_text,
                    translated_text=edited_text,
                    source_lang=segment.source_lang,
                    target_lang=segment.target_lang,
                    content_kind=ContentKind(segment.content_kind),
                    glossary_version=segment.glossary_version,
                    quality=1.0,
                )
            )
        logger.info(
            "translation.review.edited",
            review_id=review_id,
            reviewer=reviewer_id,
        )
        return review

    async def reject(
        self, review_id: str, *, reason: str | None = None, reviewer_id: str | None = None
    ) -> TranslationReview:
        """Send a segment back for re-translation."""
        review = await self._require(review_id)
        assert_transition(review.status, ReviewStatus.REJECTED)
        review.status = ReviewStatus.REJECTED
        review.reason = reason
        if reviewer_id:
            review.reviewer_id = reviewer_id
        await self._repo.session.flush()
        return review

    async def reopen(self, review_id: str) -> TranslationReview:
        """Return a review to PENDING (e.g. after a glossary change)."""
        review = await self._require(review_id)
        assert_transition(review.status, ReviewStatus.PENDING)
        review.status = ReviewStatus.PENDING
        await self._repo.session.flush()
        return review

    async def summary(self, book_id: str) -> ReviewSummary:
        """Aggregate the review states for a book."""
        reviews = await self._repo.list_reviews(book_id)
        counts: dict[ReviewStatus, int] = dict.fromkeys(ReviewStatus, 0)
        for r in reviews:
            counts[r.status] += 1
        return ReviewSummary(
            pending=counts[ReviewStatus.PENDING],
            in_review=counts[ReviewStatus.IN_REVIEW],
            edited=counts[ReviewStatus.EDITED],
            approved=counts[ReviewStatus.APPROVED],
            rejected=counts[ReviewStatus.REJECTED],
        )

    # -- helpers ---------------------------------------------------------- #

    async def _require(self, review_id: str) -> TranslationReview:
        review = await self._repo.get_review(review_id)
        if review is None:
            raise ReviewStateError(f"no such review: {review_id}")
        return review

    async def _apply_edit_to_segment(
        self, segment_row_id: str, edited_text: str
    ) -> TranslationSegment | None:
        segment = await self._repo.session.get(TranslationSegment, segment_row_id)
        if segment is None:
            return None
        segment.translated_text = edited_text
        segment.origin = TranslationOrigin.POST_EDIT.value
        segment.quality = 1.0
        segment.needs_review = False
        return segment

    async def _clear_segment_flag(self, segment_row_id: str) -> None:
        segment = await self._repo.session.get(TranslationSegment, segment_row_id)
        if segment is not None:
            segment.needs_review = False


__all__ = ["ReviewSummary", "ReviewWorkflow", "assert_transition"]
