"""Marketplace review state-machine + rating-aggregation unit tests."""

from __future__ import annotations

import pytest

from app.platform.plugins.errors import RegistryError
from app.platform.plugins.manifest import PluginManifest
from app.platform.plugins.marketplace import (
    RatingStats,
    ReviewDecision,
    ReviewStatus,
    apply_review,
    initial_review_status,
)


def _manifest(caps: list[str]) -> PluginManifest:
    return PluginManifest.parse(
        {
            "id": "com.a.p",
            "version": "1.0.0",
            "name": "P",
            "capabilities": caps,
            "hooks": [{"id": "h", "point": "ingest.filter", "entrypoint": "run"}],
        }
    )


def test_high_risk_always_pending() -> None:
    m = _manifest(["canon.write"])
    assert initial_review_status(m, auto_approve_low_risk=True) is ReviewStatus.PENDING


def test_low_risk_auto_approve_opt_in() -> None:
    m = _manifest(["book.read", "log.write"])
    assert initial_review_status(m, auto_approve_low_risk=False) is ReviewStatus.PENDING
    assert initial_review_status(m, auto_approve_low_risk=True) is ReviewStatus.APPROVED


def test_review_approve_and_yank() -> None:
    status = ReviewStatus.PENDING
    status = apply_review(status, ReviewDecision.APPROVE)
    assert status is ReviewStatus.APPROVED
    assert status.is_installable
    status = apply_review(status, ReviewDecision.YANK)
    assert status is ReviewStatus.YANKED
    assert not status.is_installable


def test_review_request_changes_then_approve() -> None:
    status = apply_review(ReviewStatus.PENDING, ReviewDecision.REQUEST_CHANGES)
    assert status is ReviewStatus.CHANGES_REQUESTED
    status = apply_review(status, ReviewDecision.APPROVE)
    assert status is ReviewStatus.APPROVED


def test_illegal_review_transition_raises() -> None:
    with pytest.raises(RegistryError):
        apply_review(ReviewStatus.REJECTED, ReviewDecision.APPROVE)
    with pytest.raises(RegistryError):
        apply_review(ReviewStatus.YANKED, ReviewDecision.APPROVE)
    with pytest.raises(RegistryError):
        apply_review(ReviewStatus.PENDING, ReviewDecision.YANK)


def test_rating_average() -> None:
    stats = RatingStats(count=0, total=0)
    assert stats.average == 0.0
    stats = stats.with_added(5).with_added(3)
    assert stats.count == 2
    assert stats.average == 4.0


def test_rating_replacement() -> None:
    stats = RatingStats(count=1, total=5)
    # User changes their 5 to a 1: count stays, sum adjusts.
    stats = stats.with_added(1, replacing=5)
    assert stats.count == 1
    assert stats.total == 1


@pytest.mark.parametrize("bad", [0, 6, -1, 2.5, True])
def test_invalid_stars_rejected(bad: object) -> None:
    with pytest.raises(RegistryError):
        RatingStats(count=0, total=0).with_added(bad)  # type: ignore[arg-type]
