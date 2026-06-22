"""The Scheduler / Prefetch Controller — generation-on-scroll (kinora.md §4).

The control plane that decides *what to render right now and what to leave cold*
— distinct from the creative Showrunner. It holds per-session reading state
(:class:`SchedulerSession`), classifies upcoming shots into committed/speculative/
cold zones (:mod:`app.scheduler.zones`), fills the committed buffer under
dual-watermark hysteresis with velocity-adaptive promotion
(:class:`SchedulerService`), debounces/dwell-confirms intent and handles seeks
(:class:`IntentController`), and maintains the cheap, zero-video keyframe lane
(:class:`KeyframeService`).
"""

from __future__ import annotations

from app.scheduler.intent import IntentController, IntentResult, SeekResult
from app.scheduler.keyframe import KeyframeResult, KeyframeService
from app.scheduler.model import (
    BufferedShot,
    SchedulerSession,
    SchedulerStore,
    new_trajectory_token,
)
from app.scheduler.service import (
    QueueKeyframeMaintainer,
    SchedulerService,
    SchedulerTick,
)
from app.scheduler.zones import (
    Zone,
    clamp_velocity,
    classify,
    eta_seconds,
    trajectory_is_stable,
)

__all__ = [
    "BufferedShot",
    "IntentController",
    "IntentResult",
    "KeyframeResult",
    "KeyframeService",
    "QueueKeyframeMaintainer",
    "SchedulerService",
    "SchedulerSession",
    "SchedulerStore",
    "SchedulerTick",
    "SeekResult",
    "Zone",
    "clamp_velocity",
    "classify",
    "eta_seconds",
    "new_trajectory_token",
    "trajectory_is_stable",
]
