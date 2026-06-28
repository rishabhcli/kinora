"""Budget-aware degradation advice from gateway metering (§11.1).

§11.1: "When ``remaining_s`` drops below a floor, the Scheduler stops promoting to
full video and rides the keyframe/Ken-Burns ladder." The gateway already meters
every video-second spent (:class:`~app.providers.resilience.metering.MeteringSink`),
so it can answer — *cheaply, with no extra call* — whether the video budget is in
the degrade zone. This module is the pure advisor; the Scheduler/render lane owns
the actual ladder.

It is deliberately read-only and side-effect-free: it never reserves budget, never
calls a provider, and never touches the ``KINORA_LIVE_VIDEO`` spend gate. It only
maps ``(spent, cap, floor)`` to a :class:`DegradationLevel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .metering import MeteringSink


class DegradationLevel(StrEnum):
    """How aggressively to ride the §12.4 ladder, by remaining-budget pressure."""

    #: Plenty of budget: full Wan video is fine.
    NONE = "none"
    #: Approaching the floor: prefer cheaper/turbo backends + animatics-first.
    SOFT = "soft"
    #: At/under the floor: stop promoting to full video; keyframe + Ken-Burns.
    HARD = "hard"


@dataclass(frozen=True, slots=True)
class BudgetWindow:
    """The video-seconds budget envelope this advisor reasons over.

    Attributes:
        cap_s: Total video-seconds allotted (the §11.1 ~1,650s free-tier pool, or a
            per-session/scene sub-allocation).
        floor_s: Remaining seconds at/below which to HARD-degrade.
        soft_margin_s: Extra headroom above the floor that triggers SOFT degrade.
    """

    cap_s: float
    floor_s: float = 0.0
    soft_margin_s: float = 0.0

    def __post_init__(self) -> None:
        if self.cap_s <= 0:
            raise ValueError("cap_s must be > 0")
        if self.floor_s < 0 or self.soft_margin_s < 0:
            raise ValueError("floor_s / soft_margin_s must be >= 0")


@dataclass(frozen=True, slots=True)
class DegradationAdvice:
    """The advisor's verdict (telemetry + the Scheduler's promotion gate)."""

    level: DegradationLevel
    remaining_s: float
    spent_s: float
    cap_s: float

    @property
    def should_degrade(self) -> bool:
        return self.level is not DegradationLevel.NONE

    @property
    def budget_low(self) -> bool:
        """The §11.1 ``budget_low`` signal the UI surfaces 'quietly'."""
        return self.level is DegradationLevel.HARD

    @property
    def fraction_spent(self) -> float:
        return min(self.spent_s / self.cap_s, 1.0) if self.cap_s else 1.0


class DegradationAdvisor:
    """Map metered video-seconds against a :class:`BudgetWindow` to advice (pure)."""

    def __init__(self, window: BudgetWindow) -> None:
        self.window = window

    def advise(self, spent_video_seconds: float) -> DegradationAdvice:
        """Verdict given the video-seconds spent so far."""
        spent = max(spent_video_seconds, 0.0)
        remaining = max(self.window.cap_s - spent, 0.0)
        if remaining <= self.window.floor_s:
            level = DegradationLevel.HARD
        elif remaining <= self.window.floor_s + self.window.soft_margin_s:
            level = DegradationLevel.SOFT
        else:
            level = DegradationLevel.NONE
        return DegradationAdvice(
            level=level,
            remaining_s=remaining,
            spent_s=spent,
            cap_s=self.window.cap_s,
        )

    def advise_from_meter(self, meter: MeteringSink) -> DegradationAdvice:
        """Convenience: read the spent video-seconds straight from a metering sink."""
        return self.advise(meter.video_seconds)


__all__ = [
    "BudgetWindow",
    "DegradationAdvice",
    "DegradationAdvisor",
    "DegradationLevel",
]
