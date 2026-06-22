"""Reading-time zones + trajectory stability (kinora.md §4.3/§4.4/§4.6).

A future shot's **ETA** is its distance ahead divided by reading velocity —
``eta = (shot.word_index_start - w) / v`` — in *seconds of reading-time*. ETA
sorts every upcoming shot into one of three zones:

* **committed** (``eta < C``)   — promote to full Wan video (spends video-seconds),
* **speculative** (``C ≤ eta ≤ SPEC``) — a cheap keyframe still only (no video),
* **cold** (``eta > SPEC``)     — plan/canon only.

Because ETA divides by ``v``, the boundaries self-tune to the reader: a faster
reader pulls distant shots across the commit horizon sooner, promoting more and
earlier (§4.6). :func:`trajectory_is_stable` suspends promotion during a rapid
skim so video-seconds are never spent on pages a skimmer will blow past.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

#: The reading-velocity default and clamp band (§4.3): 4 wps ≈ 240 wpm, [0.5×, 3×].
DEFAULT_VELOCITY_WPS = 4.0
VELOCITY_CLAMP_LOW = 0.5 * DEFAULT_VELOCITY_WPS  # 2.0 wps
VELOCITY_CLAMP_HIGH = 3.0 * DEFAULT_VELOCITY_WPS  # 12.0 wps

#: Velocity never divides as zero — a settled reader still has a tiny floor.
_MIN_VELOCITY = 0.1


class Zone(StrEnum):
    """The three generation zones, by ETA (§4.4)."""

    COMMITTED = "committed"
    SPECULATIVE = "speculative"
    COLD = "cold"


class _StabilityState(Protocol):
    """The slice of a session :func:`trajectory_is_stable` reads."""

    raw_velocity_wps: float
    oscillating: bool


def clamp_velocity(velocity_wps: float) -> float:
    """Clamp a raw velocity estimate to ``[0.5×, 3×]`` the default (§4.3)."""
    return max(VELOCITY_CLAMP_LOW, min(VELOCITY_CLAMP_HIGH, abs(velocity_wps)))


def eta_seconds(word_index_start: int, focus_word: int, velocity_wps: float) -> float:
    """ETA to a shot in reading-seconds: ``(start - w) / v`` (§4.3)."""
    v = max(abs(velocity_wps), _MIN_VELOCITY)
    return (word_index_start - focus_word) / v


def classify(eta: float, *, commit_horizon_s: float, spec_horizon_s: float) -> Zone:
    """Bucket an ETA into committed / speculative / cold (§4.4)."""
    if eta < commit_horizon_s:
        return Zone.COMMITTED
    if eta <= spec_horizon_s:
        return Zone.SPECULATIVE
    return Zone.COLD


def classify_shot(
    word_index_start: int,
    focus_word: int,
    velocity_wps: float,
    *,
    commit_horizon_s: float,
    spec_horizon_s: float,
) -> tuple[float, Zone]:
    """Return ``(eta, zone)`` for a shot at ``word_index_start`` (convenience)."""
    eta = eta_seconds(word_index_start, focus_word, velocity_wps)
    return eta, classify(eta, commit_horizon_s=commit_horizon_s, spec_horizon_s=spec_horizon_s)


def trajectory_is_stable(
    session: _StabilityState, *, clamp_high: float = VELOCITY_CLAMP_HIGH
) -> bool:
    """False during a rapid skim — velocity above the clamp ceiling or oscillating (§4.6).

    Uses the *raw* (pre-clamp) velocity estimate: ``velocity_wps`` is capped at
    the ceiling for ETA math, so skim detection reads the uncapped estimate. When
    unstable, the Scheduler suspends promotion (rides the keyframe ladder) so it
    never burns video-seconds on a skimmer's path.
    """
    if abs(session.raw_velocity_wps) > clamp_high:
        return False
    return not session.oscillating


__all__ = [
    "DEFAULT_VELOCITY_WPS",
    "VELOCITY_CLAMP_HIGH",
    "VELOCITY_CLAMP_LOW",
    "Zone",
    "clamp_velocity",
    "classify",
    "classify_shot",
    "eta_seconds",
    "trajectory_is_stable",
]
