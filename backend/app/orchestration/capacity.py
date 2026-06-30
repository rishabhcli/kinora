"""Provider-capacity oracle seam for placement (kinora.md §11.1, §12.2).

Placement must not over-commit a provider. Wan's image model already throttles
(``429 Throttling.RateQuota``); each video provider has its own in-flight ceiling
and a budget of video-seconds the governor (§11.1) is willing to spend. The
coordinator consults a :class:`CapacityOracle` before assigning a ticket so a
backed-up or budget-exhausted provider sheds new work to another lane/provider
instead of piling on.

The oracle is a *seam*, not a reimplementation of the finops governor: production
wires an adapter that reads live provider rate-limits + the governor's binding
headroom; tests wire :class:`StaticCapacityOracle` with fixed numbers. Keeping it
behind a protocol is what lets the coordinator's placement policy be unit-tested
deterministically without the budget service or any network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.orchestration.models import ProviderId

__all__ = ["ProviderCapacity", "CapacityOracle", "StaticCapacityOracle"]


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    """A provider's current admission headroom.

    ``max_inflight`` is the concurrency ceiling (rate-limit aware); ``inflight`` is
    how many leases the coordinator currently has out against it; ``video_seconds``
    is the remaining spend the governor permits (``inf`` when the live gate is off
    and Ken-Burns is free, or when no budget binds).
    """

    provider: ProviderId
    max_inflight: int
    inflight: int = 0
    video_seconds_headroom: float = float("inf")

    @property
    def slots_free(self) -> int:
        """Concurrency slots still open on this provider (never negative)."""
        return max(0, self.max_inflight - self.inflight)

    def admits(self, *, video_seconds: float) -> bool:
        """True if one more shot of ``video_seconds`` fits within both ceilings."""
        if self.slots_free <= 0:
            return False
        return video_seconds <= self.video_seconds_headroom


class CapacityOracle(Protocol):
    """Answers 'does this provider have room for one more shot right now?'."""

    def capacity_for(self, provider: ProviderId) -> ProviderCapacity:
        """The current :class:`ProviderCapacity` for ``provider``."""
        ...

    def note_assigned(self, provider: ProviderId, *, video_seconds: float) -> None:
        """Record that a shot was just assigned (decrement headroom)."""
        ...

    def note_released(self, provider: ProviderId, *, video_seconds: float) -> None:
        """Record that a shot completed/cancelled (return headroom)."""
        ...


class StaticCapacityOracle:
    """A test/dev oracle holding fixed ceilings + mutable in-flight counters.

    Construct with the per-provider ``max_inflight`` (and optional video-seconds
    headroom); the coordinator drives ``note_assigned`` / ``note_released`` so the
    in-flight count tracks live placement. Unknown providers default to a single
    slot with infinite video headroom (a conservative "exists but tiny" guess).
    """

    def __init__(
        self,
        *,
        max_inflight: dict[ProviderId, int] | None = None,
        video_seconds_headroom: dict[ProviderId, float] | None = None,
        default_max_inflight: int = 1,
    ) -> None:
        self._max = dict(max_inflight or {})
        self._vsh = dict(video_seconds_headroom or {})
        self._default_max = default_max_inflight
        self._inflight: dict[ProviderId, int] = {}
        self._spent: dict[ProviderId, float] = {}

    def capacity_for(self, provider: ProviderId) -> ProviderCapacity:
        max_inflight = self._max.get(provider, self._default_max)
        headroom = self._vsh.get(provider, float("inf"))
        spent = self._spent.get(provider, 0.0)
        remaining = headroom - spent if headroom != float("inf") else float("inf")
        return ProviderCapacity(
            provider=provider,
            max_inflight=max_inflight,
            inflight=self._inflight.get(provider, 0),
            video_seconds_headroom=remaining,
        )

    def note_assigned(self, provider: ProviderId, *, video_seconds: float) -> None:
        self._inflight[provider] = self._inflight.get(provider, 0) + 1
        self._spent[provider] = self._spent.get(provider, 0.0) + max(0.0, video_seconds)

    def note_released(self, provider: ProviderId, *, video_seconds: float) -> None:
        self._inflight[provider] = max(0, self._inflight.get(provider, 0) - 1)
        self._spent[provider] = max(0.0, self._spent.get(provider, 0.0) - max(0.0, video_seconds))


@dataclass
class _UnboundedCapacityOracle:
    """An oracle that always admits (no provider limits) — handy as a default."""

    _inflight: dict[ProviderId, int] = field(default_factory=dict)

    def capacity_for(self, provider: ProviderId) -> ProviderCapacity:
        return ProviderCapacity(
            provider=provider,
            max_inflight=2**31,
            inflight=self._inflight.get(provider, 0),
        )

    def note_assigned(self, provider: ProviderId, *, video_seconds: float) -> None:
        self._inflight[provider] = self._inflight.get(provider, 0) + 1

    def note_released(self, provider: ProviderId, *, video_seconds: float) -> None:
        self._inflight[provider] = max(0, self._inflight.get(provider, 0) - 1)
