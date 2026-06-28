"""Per-workspace quotas + seat management — pure policy helpers.

Quotas live in two places: an organization's purchased ``seats`` (the cap on
distinct active members across its workspaces) and a workspace's ``settings``
JSONB bag (per-workspace knobs like the maximum number of attached books and a
video-seconds cap). Keeping the *interpretation* of those knobs here — pure
functions over plain data — means the service layer can enforce them and tests
can exercise every boundary without infra.

A ``0`` seat count or a missing/``None`` quota means **unlimited** (the design's
fail-open default for an unset knob); a positive value is a hard cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.workspaces.roles import OrgPlan, Role

#: Default seats granted by each org plan (overridable per-org via ``seats``).
PLAN_DEFAULT_SEATS: dict[OrgPlan, int] = {
    OrgPlan.FREE: 3,
    OrgPlan.TEAM: 25,
    OrgPlan.ENTERPRISE: 0,  # 0 == unlimited
}

#: Workspace settings keys the quota layer understands.
SETTING_MAX_BOOKS = "max_books"
SETTING_VIDEO_SECONDS_CAP = "video_seconds_cap"
SETTING_DEFAULT_MEMBER_ROLE = "default_member_role"
SETTING_ALLOW_PUBLIC_SHARE = "allow_public_share"


class QuotaExceeded(Exception):  # noqa: N818 - "*Exceeded" matches BudgetExceeded
    """Raised when an operation would push a workspace/org past a quota."""

    def __init__(self, *, quota: str, limit: int, used: int) -> None:
        super().__init__(f"{quota} quota exceeded ({used}/{limit})")
        self.quota = quota
        self.limit = limit
        self.used = used


@dataclass(frozen=True, slots=True)
class SeatUsage:
    """A snapshot of an organization's seat consumption."""

    seats: int  # 0 == unlimited
    active_members: int

    @property
    def unlimited(self) -> bool:
        return self.seats <= 0

    @property
    def available(self) -> int:
        """Remaining seats (a large sentinel when unlimited)."""
        if self.unlimited:
            return 1_000_000
        return max(0, self.seats - self.active_members)

    def can_add(self, count: int = 1) -> bool:
        """True when ``count`` more active members would still fit."""
        if self.unlimited:
            return True
        return self.active_members + count <= self.seats


def default_seats_for_plan(plan: OrgPlan) -> int:
    """Default seat count for a plan tier (0 == unlimited)."""
    return PLAN_DEFAULT_SEATS.get(plan, PLAN_DEFAULT_SEATS[OrgPlan.FREE])


def _as_positive_int(value: Any) -> int | None:
    """Coerce a settings value to a positive int cap, or ``None`` (== unlimited)."""
    if value is None:
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def max_books_for(settings: dict[str, Any] | None) -> int | None:
    """The book cap from a workspace's settings (``None`` == unlimited)."""
    if not settings:
        return None
    return _as_positive_int(settings.get(SETTING_MAX_BOOKS))


def video_seconds_cap_for(settings: dict[str, Any] | None) -> int | None:
    """The video-seconds cap from a workspace's settings (``None`` == unlimited)."""
    if not settings:
        return None
    return _as_positive_int(settings.get(SETTING_VIDEO_SECONDS_CAP))


def default_member_role_for(settings: dict[str, Any] | None) -> Role:
    """The default role a new member/invitee gets (defaults to VIEWER)."""
    if settings:
        raw = settings.get(SETTING_DEFAULT_MEMBER_ROLE)
        if isinstance(raw, str):
            try:
                return Role(raw)
            except ValueError:
                pass
    return Role.VIEWER


def check_book_quota(settings: dict[str, Any] | None, current_books: int) -> None:
    """Raise :class:`QuotaExceeded` if adding one more book would exceed the cap."""
    cap = max_books_for(settings)
    if cap is not None and current_books >= cap:
        raise QuotaExceeded(quota=SETTING_MAX_BOOKS, limit=cap, used=current_books)


def check_seat_quota(usage: SeatUsage, *, adding: int = 1) -> None:
    """Raise :class:`QuotaExceeded` if ``adding`` members would exceed the seats."""
    if not usage.can_add(adding):
        raise QuotaExceeded(
            quota="seats", limit=usage.seats, used=usage.active_members + adding
        )


__all__ = [
    "PLAN_DEFAULT_SEATS",
    "SETTING_ALLOW_PUBLIC_SHARE",
    "SETTING_DEFAULT_MEMBER_ROLE",
    "SETTING_MAX_BOOKS",
    "SETTING_VIDEO_SECONDS_CAP",
    "QuotaExceeded",
    "SeatUsage",
    "check_book_quota",
    "check_seat_quota",
    "default_member_role_for",
    "default_seats_for_plan",
    "max_books_for",
    "video_seconds_cap_for",
]
