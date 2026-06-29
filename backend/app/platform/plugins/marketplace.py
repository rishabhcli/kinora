"""Marketplace domain types + publish/review/rating policy (pure).

The marketplace is the *registry of published artifacts* plus the moderation and
social signals around them. This module holds the pure value objects and policy
functions; persistence lives in :mod:`app.platform.plugins.store` and
orchestration in :mod:`app.platform.plugins.service`.

The publish/review state machine for an artifact:

```
            publish                 approve
   (none) ---------> PENDING ------------------> APPROVED
                       |  ^                          |
              reject   |  | request_changes          | yank
                       v  |                          v
                    REJECTED <-- CHANGES_REQUESTED   YANKED
```

* A freshly published artifact is ``PENDING`` (or auto-``APPROVED`` if it is
  low-risk and the host policy allows auto-approval — see
  :func:`initial_review_status`).
* Only an ``APPROVED`` (and non-yanked) artifact is installable by new tenants.
* Ratings aggregate into an average exposed on the catalog listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.platform.plugins.capabilities import RiskTier
from app.platform.plugins.errors import RegistryError
from app.platform.plugins.manifest import PluginManifest


class ReviewStatus(StrEnum):
    """The moderation state of a published artifact."""

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    YANKED = "yanked"

    @property
    def is_installable(self) -> bool:
        """Only an approved (non-yanked) artifact may be freshly installed."""
        return self is ReviewStatus.APPROVED


class ReviewDecision(StrEnum):
    """A reviewer's action on a pending artifact."""

    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
    YANK = "yank"


#: Legal (status -> {decisions}) review transitions.
_REVIEW_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewDecision]] = {
    ReviewStatus.PENDING: frozenset(
        {ReviewDecision.APPROVE, ReviewDecision.REJECT, ReviewDecision.REQUEST_CHANGES}
    ),
    ReviewStatus.CHANGES_REQUESTED: frozenset({ReviewDecision.APPROVE, ReviewDecision.REJECT}),
    ReviewStatus.APPROVED: frozenset({ReviewDecision.YANK, ReviewDecision.REJECT}),
    ReviewStatus.REJECTED: frozenset(),
    ReviewStatus.YANKED: frozenset(),
}

#: Maps a decision to the status it produces.
_DECISION_RESULT: dict[ReviewDecision, ReviewStatus] = {
    ReviewDecision.APPROVE: ReviewStatus.APPROVED,
    ReviewDecision.REJECT: ReviewStatus.REJECTED,
    ReviewDecision.REQUEST_CHANGES: ReviewStatus.CHANGES_REQUESTED,
    ReviewDecision.YANK: ReviewStatus.YANKED,
}


def initial_review_status(
    manifest: PluginManifest, *, auto_approve_low_risk: bool = False
) -> ReviewStatus:
    """The status a freshly published artifact starts in.

    High-risk plugins (those requesting HIGH capabilities) always require manual
    review. Low-risk plugins may be auto-approved when the host opts in
    (``auto_approve_low_risk``) — useful for first-party / trusted publishers.
    """
    if manifest.requires_review:
        return ReviewStatus.PENDING
    if auto_approve_low_risk and manifest.max_risk is RiskTier.LOW:
        return ReviewStatus.APPROVED
    return ReviewStatus.PENDING


def apply_review(current: ReviewStatus, decision: ReviewDecision) -> ReviewStatus:
    """Apply a reviewer ``decision`` to ``current`` (raises on illegal move)."""
    allowed = _REVIEW_TRANSITIONS.get(current, frozenset())
    if decision not in allowed:
        raise RegistryError(f"cannot {decision.value} an artifact in status {current.value!r}")
    return _DECISION_RESULT[decision]


@dataclass(frozen=True, slots=True)
class RatingStats:
    """Aggregate rating signal for a plugin (across all its versions)."""

    count: int
    total: int

    @property
    def average(self) -> float:
        """The mean star rating in ``[0, 5]`` (``0.0`` when unrated)."""
        return round(self.total / self.count, 3) if self.count else 0.0

    def with_added(self, stars: int, *, replacing: int | None = None) -> RatingStats:
        """Return updated stats after adding ``stars`` (optionally replacing one).

        When a user re-rates, the old value is supplied via ``replacing`` so the
        running sum stays correct without recomputing from all rows.
        """
        _validate_stars(stars)
        if replacing is None:
            return RatingStats(count=self.count + 1, total=self.total + stars)
        _validate_stars(replacing)
        return RatingStats(count=self.count, total=self.total - replacing + stars)


def _validate_stars(stars: int) -> None:
    if not isinstance(stars, int) or isinstance(stars, bool) or not 1 <= stars <= 5:
        raise RegistryError(f"rating must be an integer 1..5, got {stars!r}")


@dataclass(frozen=True, slots=True)
class CatalogListing:
    """A public marketplace listing (one plugin, its latest approved version)."""

    plugin_id: str
    name: str
    publisher: str
    latest_version: str
    status: ReviewStatus
    max_risk: RiskTier
    signed: bool
    rating: RatingStats
    install_count: int
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "publisher": self.publisher,
            "latest_version": self.latest_version,
            "status": self.status.value,
            "max_risk": self.max_risk.value,
            "signed": self.signed,
            "rating_average": self.rating.average,
            "rating_count": self.rating.count,
            "install_count": self.install_count,
            "description": self.description,
        }


__all__ = [
    "CatalogListing",
    "RatingStats",
    "ReviewDecision",
    "ReviewStatus",
    "apply_review",
    "initial_review_status",
]
