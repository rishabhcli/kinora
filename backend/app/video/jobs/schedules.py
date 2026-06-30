"""Provider-specific poll cadences + the overall job deadline.

Different hosted providers want different poll rhythms: DashScope/Wan tasks take
tens of seconds to minutes (poll every few seconds, ramping), MiniMax is similar
but a touch faster. This module centralizes those as named
:class:`PollProfile`s and adapts the repo's deterministic
:class:`~app.providers.resilience.backoff.BackoffSchedule` (injectable RNG, never
reads the clock) to the engine's :class:`~app.video.jobs.ports.PollSchedule`
protocol — so the whole poll loop stays reproducible under a fake clock.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.providers.resilience.backoff import BackoffPolicy, BackoffSchedule, JitterStrategy


@dataclass(frozen=True, slots=True)
class PollProfile:
    """A named poll cadence + overall deadline for one provider family."""

    name: str
    #: Delay before the first re-poll after submit.
    base_s: float
    #: Ceiling on a single inter-poll delay.
    max_interval_s: float
    #: Growth factor between polls.
    multiplier: float
    #: Hard wall-clock budget for the whole job; exceeding it ⇒ EXPIRED.
    deadline_s: float
    strategy: JitterStrategy = JitterStrategy.EQUAL

    def schedule(self, *, rng: random.Random | None = None) -> BackoffSchedule:
        """Build a fresh, stateful backoff schedule for one job's poll loop."""
        return BackoffSchedule(
            BackoffPolicy(
                base_s=self.base_s,
                max_s=self.max_interval_s,
                multiplier=self.multiplier,
                strategy=self.strategy,
                respect_retry_after=True,
            ),
            rng=rng,
        )


#: DashScope / Wan: minutes-long renders; ramp from 3s to 15s, 10-minute budget.
DASHSCOPE_PROFILE = PollProfile(
    name="dashscope",
    base_s=3.0,
    max_interval_s=15.0,
    multiplier=1.5,
    deadline_s=600.0,
)

#: MiniMax (Hailuo): comparable cadence, slightly tighter ceiling + budget.
MINIMAX_PROFILE = PollProfile(
    name="minimax",
    base_s=2.0,
    max_interval_s=10.0,
    multiplier=1.5,
    deadline_s=480.0,
)

#: Fallback for an unknown provider id.
DEFAULT_PROFILE = PollProfile(
    name="default",
    base_s=3.0,
    max_interval_s=15.0,
    multiplier=1.5,
    deadline_s=600.0,
)

_PROFILES: dict[str, PollProfile] = {
    "dashscope": DASHSCOPE_PROFILE,
    "wan": DASHSCOPE_PROFILE,
    "minimax": MINIMAX_PROFILE,
}


def profile_for(provider: str) -> PollProfile:
    """Resolve the poll profile for a provider id (case-insensitive prefix match)."""
    key = provider.lower()
    if key in _PROFILES:
        return _PROFILES[key]
    for known, profile in _PROFILES.items():
        if key.startswith(known):
            return profile
    return DEFAULT_PROFILE


__all__ = [
    "DASHSCOPE_PROFILE",
    "DEFAULT_PROFILE",
    "MINIMAX_PROFILE",
    "PollProfile",
    "profile_for",
]
