"""Pluggable selection policies for the v2 video router.

A :class:`SelectionPolicy` answers one question: *given the backends whose
circuit breakers currently allow a call, in what order should the router try them
for this render?* It returns a ranked list of backend **names** (best first);
the router then attempts them in that order (failover) or starts the top-``k``
concurrently (hedge/race).

Every policy is a pure function of a :class:`RouteContext` snapshot — candidate
names, the :class:`~app.video.routing.capabilities.ProfileBook`, a read-only
health view, the requested :class:`~app.providers.types.WanMode`, and a
``budget_low`` flag — so ordering is deterministic and exhaustively testable with
no network and no real backends.

Concrete policies:

* :class:`CapabilityFilteredPolicy` — a *wrapper* that drops backends which can't
  render the requested mode, then defers to an inner policy. The router always
  applies this so a policy never picks an incapable backend.
* :class:`CheapestCapablePolicy` — ascending ``cost_per_s`` (preserve scarce
  video-seconds, §11).
* :class:`FastestPolicy` — ascending observed p95 latency (falling back to the
  static ``est_latency_s`` hint before any latency is observed).
* :class:`HighestQualityPolicy` — descending ``quality``.
* :class:`WeightedBlendPolicy` — a tunable linear blend of cost, quality, latency,
  success-rate, and static weight; the general-purpose default.

Ties always fall back to the input (priority) order via a stable sort, so a
policy with no signal degrades to plain priority ordering.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.providers.types import WanMode

from .capabilities import ProfileBook, filter_capable


@runtime_checkable
class HealthView(Protocol):
    """The read-only health signals a policy may consult for a backend name."""

    def success_rate(self, name: str) -> float: ...

    def p50_latency_ms(self, name: str) -> float: ...

    def p95_latency_ms(self, name: str) -> float: ...


@dataclass(frozen=True, slots=True)
class RouteContext:
    """The immutable snapshot a :class:`SelectionPolicy` ranks over.

    Attributes:
        candidates: Backend names whose breaker currently permits a call, already
            in priority (construction) order.
        profiles: Cost/quality/latency/capability lookup.
        health: Read-only live health signals (success-rate, latencies).
        mode: The render mode being routed (for capability filtering).
        budget_low: True when scarce video-seconds are running low (policies may
            shift toward cheaper backends).
    """

    candidates: tuple[str, ...]
    profiles: ProfileBook
    health: HealthView
    mode: WanMode
    budget_low: bool = False


@runtime_checkable
class SelectionPolicy(Protocol):
    """Rank routable backends best-first for one render (pure)."""

    @property
    def name(self) -> str:
        """Stable identity for telemetry (logged with every routing decision)."""
        ...

    def rank(self, ctx: RouteContext) -> list[str]:
        """Return ``ctx.candidates`` reordered best-first (may drop entries)."""
        ...


def _stable_sort(candidates: Sequence[str], key: Callable[[str], float]) -> list[str]:
    """Stable sort that keeps input order on ties (priority is the tie-breaker)."""
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda item: (key(item[1]), item[0]))
    return [name for _, name in indexed]


# --------------------------------------------------------------------------- #
# Capability filtering (a wrapper the router always applies)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CapabilityFilteredPolicy:
    """Drop backends that can't render the mode, then defer to ``inner``.

    The router wraps the configured policy in this so a selection can never return
    a backend whose profile excludes the requested :class:`WanMode`. If filtering
    removes *every* candidate (no capable backend), it returns an empty list and
    the router surfaces a clear capability error.
    """

    inner: SelectionPolicy

    @property
    def name(self) -> str:
        return f"capability_filtered({self.inner.name})"

    def rank(self, ctx: RouteContext) -> list[str]:
        capable = filter_capable(ctx.candidates, ctx.profiles, ctx.mode)
        if not capable:
            return []
        inner_ctx = RouteContext(
            candidates=tuple(capable),
            profiles=ctx.profiles,
            health=ctx.health,
            mode=ctx.mode,
            budget_low=ctx.budget_low,
        )
        return self.inner.rank(inner_ctx)


# --------------------------------------------------------------------------- #
# Concrete ordering policies
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CheapestCapablePolicy:
    """Order by ascending ``cost_per_s`` (cheapest first)."""

    name: str = "cheapest_capable"

    def rank(self, ctx: RouteContext) -> list[str]:
        return _stable_sort(
            ctx.candidates,
            key=lambda n: ctx.profiles.get(n).cost_per_s,
        )


@dataclass(frozen=True, slots=True)
class FastestPolicy:
    """Order by ascending latency.

    Uses the observed p95 latency once a backend has recorded any; falls back to
    the static ``est_latency_s`` hint (in ms) so a never-yet-used backend still
    has a sane initial position rather than sorting to 0 and always winning.
    """

    name: str = "fastest"

    def rank(self, ctx: RouteContext) -> list[str]:
        def latency_ms(n: str) -> float:
            observed = ctx.health.p95_latency_ms(n)
            if observed > 0:
                return observed
            return ctx.profiles.get(n).est_latency_s * 1000.0

        return _stable_sort(ctx.candidates, key=latency_ms)


@dataclass(frozen=True, slots=True)
class HighestQualityPolicy:
    """Order by descending ``quality`` (best fidelity first)."""

    name: str = "highest_quality"

    def rank(self, ctx: RouteContext) -> list[str]:
        return _stable_sort(
            ctx.candidates,
            key=lambda n: -ctx.profiles.get(n).quality,
        )


@dataclass(frozen=True, slots=True)
class WeightedBlendPolicy:
    """A tunable linear blend of cost, quality, latency, success-rate, and weight.

    Each backend gets a score; higher = preferred. The score rewards quality,
    success-rate, and static weight, and penalizes cost and latency. When
    ``budget_low`` is set the cost penalty is amplified by ``budget_low_cost_boost``
    so the blend leans cheaper under budget pressure (without fully collapsing to
    cheapest-only). Latency uses observed p95 (ms→s) when present, else the static
    hint. All terms are normalized to a roughly comparable 0..1 range so the
    weights are interpretable.

    The router sorts descending by score; ties keep priority order.
    """

    name: str = "weighted_blend"
    cost_weight: float = 0.3
    quality_weight: float = 0.35
    latency_weight: float = 0.2
    success_weight: float = 0.15
    #: Reference latency (s) that maps to a full latency penalty of 1.0.
    latency_ref_s: float = 120.0
    #: Reference cost that maps to a full cost penalty of 1.0.
    cost_ref: float = 8.0
    #: Multiplier applied to the cost penalty when ``budget_low``.
    budget_low_cost_boost: float = 2.0

    def _score(self, name: str, ctx: RouteContext) -> float:
        profile = ctx.profiles.get(name)
        # Latency in seconds: observed p95 if present, else the static hint.
        observed_ms = ctx.health.p95_latency_ms(name)
        latency_s = (observed_ms / 1000.0) if observed_ms > 0 else profile.est_latency_s
        latency_penalty = min(1.0, latency_s / self.latency_ref_s) if self.latency_ref_s else 0.0
        cost_penalty = min(1.0, profile.cost_per_s / self.cost_ref) if self.cost_ref else 0.0
        if ctx.budget_low:
            cost_penalty = min(1.0, cost_penalty * self.budget_low_cost_boost)
        success = ctx.health.success_rate(name)
        # The static weight nudges otherwise-equal backends; fold it in gently as a
        # small additive bonus normalized by itself so weight=1 is neutral-ish.
        weight_bonus = 0.05 * (profile.weight - 1.0)
        return (
            self.quality_weight * profile.quality
            + self.success_weight * success
            - self.cost_weight * cost_penalty
            - self.latency_weight * latency_penalty
            + weight_bonus
        )

    def rank(self, ctx: RouteContext) -> list[str]:
        # Sort descending by score (negate for the ascending stable sort).
        return _stable_sort(ctx.candidates, key=lambda n: -self._score(n, ctx))


class PolicyKind(StrEnum):
    """Named built-in policies, for config-driven selection."""

    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    HIGHEST_QUALITY = "highest_quality"
    WEIGHTED = "weighted"


def build_policy(kind: PolicyKind | str) -> SelectionPolicy:
    """Construct a built-in :class:`SelectionPolicy` by :class:`PolicyKind`."""
    resolved = PolicyKind(kind)
    builders: dict[PolicyKind, SelectionPolicy] = {
        PolicyKind.CHEAPEST: CheapestCapablePolicy(),
        PolicyKind.FASTEST: FastestPolicy(),
        PolicyKind.HIGHEST_QUALITY: HighestQualityPolicy(),
        PolicyKind.WEIGHTED: WeightedBlendPolicy(),
    }
    return builders[resolved]


__all__ = [
    "CapabilityFilteredPolicy",
    "CheapestCapablePolicy",
    "FastestPolicy",
    "HealthView",
    "HighestQualityPolicy",
    "PolicyKind",
    "RouteContext",
    "SelectionPolicy",
    "WeightedBlendPolicy",
    "build_policy",
]
