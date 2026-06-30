"""Per-provider capability + cost/quality/latency profiles for selection.

A :class:`VideoBackend` only exposes ``name`` / ``render`` / ``healthy`` — it says
nothing about *which* :class:`~app.providers.types.WanMode` s it can render, how
much a video-second costs there, or how fast/good it is. The selection policies
need that, so the router carries a :class:`ProviderProfile` per backend (keyed by
backend ``name``).

Profiles are pure data with sensible neutral defaults: an unprofiled backend is
assumed to support *every* mode at neutral cost/quality/latency, so a router with
no profiles configured degrades gracefully to ordering by health alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.providers.types import WanMode

#: Every render mode — the default "supports everything" capability set.
ALL_MODES: frozenset[WanMode] = frozenset(WanMode)


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Cost / quality / latency / capability declaration for one backend.

    Attributes:
        modes: The :class:`WanMode` s this backend can render. Empty/omitted →
            :data:`ALL_MODES` (assume fully capable). A capability-filtered policy
            drops any backend whose ``modes`` excludes the requested mode.
        cost_per_s: Relative cost of one video-second here (ratios matter, not
            absolute units): a turbo id is cheaper than a quality id.
        quality: 0..1 fidelity score — higher is better (quality id > turbo).
        est_latency_s: A static latency hint (seconds) used to seed the "fastest"
            policy before any live p50 latency has been observed.
        weight: A static priority weight (>0) for the weighted-blend policy's
            preference term; higher = more preferred, all else equal.
    """

    modes: frozenset[WanMode] = ALL_MODES
    cost_per_s: float = 1.0
    quality: float = 0.5
    est_latency_s: float = 30.0
    weight: float = 1.0

    def __post_init__(self) -> None:
        if self.cost_per_s < 0:
            raise ValueError("cost_per_s must be >= 0")
        if not (0.0 <= self.quality <= 1.0):
            raise ValueError("quality must be in [0, 1]")
        if self.est_latency_s < 0:
            raise ValueError("est_latency_s must be >= 0")
        if self.weight <= 0:
            raise ValueError("weight must be > 0")

    def supports(self, mode: WanMode) -> bool:
        """True when this backend can render ``mode``."""
        return mode in self.modes


#: The profile assigned to any backend with no explicit entry (fully capable,
#: neutral cost/quality/latency/weight).
NEUTRAL_PROFILE = ProviderProfile()


@dataclass(frozen=True, slots=True)
class ProfileBook:
    """An immutable name → :class:`ProviderProfile` lookup with a neutral default.

    Centralizes "what do we know about backend X" so every policy reads profiles
    the same way and an unprofiled backend always resolves to :data:`NEUTRAL_PROFILE`
    instead of raising.
    """

    profiles: Mapping[str, ProviderProfile] = field(default_factory=dict)

    def get(self, name: str) -> ProviderProfile:
        """The profile for backend ``name`` (or :data:`NEUTRAL_PROFILE`)."""
        return self.profiles.get(name, NEUTRAL_PROFILE)

    def supports(self, name: str, mode: WanMode) -> bool:
        """True when backend ``name`` can render ``mode`` (neutral = all modes)."""
        return self.get(name).supports(mode)


def normalize_profiles(
    profiles: Mapping[str, ProviderProfile] | None,
) -> ProfileBook:
    """Build a :class:`ProfileBook` from a (possibly ``None``) profile mapping."""
    return ProfileBook(profiles=dict(profiles or {}))


def filter_capable(
    names: Iterable[str],
    book: ProfileBook,
    mode: WanMode,
) -> list[str]:
    """Keep only backend names whose profile supports ``mode`` (order preserved)."""
    return [name for name in names if book.supports(name, mode)]


__all__ = [
    "ALL_MODES",
    "NEUTRAL_PROFILE",
    "ProfileBook",
    "ProviderProfile",
    "filter_capable",
    "normalize_profiles",
]
