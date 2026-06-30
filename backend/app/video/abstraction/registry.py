"""The :class:`ProviderRegistry` — register / look up video providers by id and
capability query.

The Scheduler/Generator never names a model id directly (§9.2/§9.3); it asks the
registry "give me a provider that can do reference_to_video at 720p for 5s" and
the registry returns the matching providers, optionally ranked by a selection
policy (cheapest-first to preserve scarce video-seconds, or best-quality-first).

The registry is a *pure in-memory index* — no I/O, no env reads. It is the DI
seam the composition root populates (one entry per hosted region / lane), and a
test populates with deterministic fakes. Lookups are stable and deterministic:
ties always resolve to registration order.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger

from .capability import CapabilityQuery, VideoCapability
from .provider import UniversalVideoProvider

logger = get_logger("app.video.abstraction.registry")


class ProviderNotFound(LookupError):  # noqa: N818 — intuitive name; subclasses LookupError
    """No registered provider matches the requested id or capability query."""


class DuplicateProvider(ValueError):  # noqa: N818 — intuitive name; subclasses ValueError
    """A provider with the same id is already registered (and overwrite=False)."""


class SelectionStrategy(StrEnum):
    """How :meth:`ProviderRegistry.select` ranks the providers that match a query."""

    #: Keep registration order (first registered = preferred). Deterministic.
    PRIORITY = "priority"
    #: Cheapest video-second first — preserve budget (§11.1). Needs a cost map.
    CHEAPEST = "cheapest"
    #: Best declared quality first. Needs a quality map.
    BEST_QUALITY = "best_quality"
    #: Shortest min-duration first — favours providers that can do tiny clips.
    SHORTEST_FLOOR = "shortest_floor"


@dataclass(frozen=True, slots=True)
class ProviderRanking:
    """Per-provider cost/quality hints for ranked selection (pure data).

    Absolute units are irrelevant — only the ordering matters. Providers absent
    from a map sort to a neutral middle so a partial map still ranks sensibly.
    """

    cost_per_s: float = 1.0
    quality: float = 0.5


@dataclass
class ProviderRegistry:
    """An ordered, in-memory registry of :class:`UniversalVideoProvider` s.

    Registration order is the default priority. ``rankings`` supply optional
    cost/quality positions for the cost-/quality-aware selection strategies; they
    are pure metadata and never trigger I/O.
    """

    _providers: dict[str, UniversalVideoProvider] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    rankings: dict[str, ProviderRanking] = field(default_factory=dict)

    # -- registration ----------------------------------------------------- #

    def register(
        self,
        provider: UniversalVideoProvider,
        *,
        ranking: ProviderRanking | None = None,
        overwrite: bool = False,
    ) -> None:
        """Register ``provider`` under its ``provider_id``.

        The provider's ``provider_id`` MUST equal ``capabilities().provider_id``
        (a self-consistency guard caught at registration, not at routing time).

        Raises:
            DuplicateProvider: the id is taken and ``overwrite`` is False.
            ValueError: the provider's id disagrees with its capability id.
        """
        pid = provider.provider_id
        cap_pid = provider.capabilities().provider_id
        if pid != cap_pid:
            raise ValueError(
                f"provider_id {pid!r} != capabilities().provider_id {cap_pid!r}"
            )
        if pid in self._providers and not overwrite:
            raise DuplicateProvider(f"provider {pid!r} already registered")
        if pid not in self._providers:
            self._order.append(pid)
        self._providers[pid] = provider
        if ranking is not None:
            self.rankings[pid] = ranking
        logger.info("video_registry.registered", provider_id=pid, overwrite=overwrite)

    def unregister(self, provider_id: str) -> None:
        """Remove a provider by id (no-op if absent)."""
        self._providers.pop(provider_id, None)
        self.rankings.pop(provider_id, None)
        if provider_id in self._order:
            self._order.remove(provider_id)

    # -- direct lookup ---------------------------------------------------- #

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def ids(self) -> tuple[str, ...]:
        """Registered provider ids in registration (priority) order."""
        return tuple(self._order)

    def get(self, provider_id: str) -> UniversalVideoProvider:
        """Return the provider registered under ``provider_id``.

        Raises:
            ProviderNotFound: no provider with that id.
        """
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ProviderNotFound(f"no provider registered as {provider_id!r}") from exc

    def all(self) -> tuple[UniversalVideoProvider, ...]:
        """Every registered provider in priority order."""
        return tuple(self._providers[pid] for pid in self._order)

    def capabilities(self) -> Mapping[str, VideoCapability]:
        """A snapshot mapping of ``provider_id`` → declared capability."""
        return {pid: self._providers[pid].capabilities() for pid in self._order}

    # -- capability query ------------------------------------------------- #

    def find(self, query: CapabilityQuery) -> tuple[UniversalVideoProvider, ...]:
        """Every provider whose capability :meth:`supports` ``query`` (priority order)."""
        return tuple(
            self._providers[pid]
            for pid in self._order
            if self._providers[pid].capabilities().supports(query)
        )

    def select(
        self,
        query: CapabilityQuery,
        *,
        strategy: SelectionStrategy = SelectionStrategy.PRIORITY,
    ) -> UniversalVideoProvider:
        """Return the single best provider satisfying ``query`` under ``strategy``.

        Ranking is a stable sort over a deterministic key, so ties always resolve
        to registration (priority) order — the same query always picks the same
        provider given the same registry.

        Raises:
            ProviderNotFound: nothing matches the query.
        """
        candidates = self.find(query)
        if not candidates:
            raise ProviderNotFound(f"no provider satisfies query {query!r}")
        ranked = self.rank(candidates, strategy=strategy)
        chosen = ranked[0]
        logger.info(
            "video_registry.selected",
            provider_id=chosen.provider_id,
            strategy=strategy.value,
            candidates=len(candidates),
        )
        return chosen

    def rank(
        self,
        providers: Iterable[UniversalVideoProvider],
        *,
        strategy: SelectionStrategy = SelectionStrategy.PRIORITY,
    ) -> tuple[UniversalVideoProvider, ...]:
        """Order ``providers`` by ``strategy`` (stable; ties → priority order)."""
        items = list(providers)
        priority = {pid: i for i, pid in enumerate(self._order)}
        neutral = ProviderRanking()

        def key(p: UniversalVideoProvider) -> tuple[float, int]:
            idx = priority.get(p.provider_id, len(priority))
            rank = self.rankings.get(p.provider_id, neutral)
            primary = self._primary_key(p, rank, strategy)
            return (primary, idx)

        return tuple(sorted(items, key=key))

    @staticmethod
    def _primary_key(
        provider: UniversalVideoProvider,
        rank: ProviderRanking,
        strategy: SelectionStrategy,
    ) -> float:
        """The strategy's primary sort value (lower sorts first)."""
        selectors: dict[SelectionStrategy, Callable[[], float]] = {
            SelectionStrategy.PRIORITY: lambda: 0.0,
            SelectionStrategy.CHEAPEST: lambda: rank.cost_per_s,
            SelectionStrategy.BEST_QUALITY: lambda: -rank.quality,
            SelectionStrategy.SHORTEST_FLOOR: lambda: provider.capabilities().min_duration_s,
        }
        return selectors[strategy]()


__all__ = [
    "DuplicateProvider",
    "ProviderNotFound",
    "ProviderRanking",
    "ProviderRegistry",
    "SelectionStrategy",
]
