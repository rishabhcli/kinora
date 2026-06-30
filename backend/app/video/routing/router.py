"""The v2 multi-provider video router — a drop-in :class:`VideoBackend`.

:class:`RoutingVideoRouter` sits over N :class:`~app.providers.video_router.VideoBackend`
transports and implements the same ``name`` / ``render`` / ``healthy`` contract,
so the existing Generator / render pipeline use it unchanged (it can even nest
inside the round-1 ``VideoRouter`` or vice-versa). On top of the round-1 router it
adds, per the v2 brief:

* **Health tracking + circuit breakers** (:mod:`.health`): rolling success-rate,
  p50/p95 latency, an error-class histogram, and a breaker with *exponential*
  cooldown — consulted for both gating and policy selection.
* **Pluggable selection policies** (:mod:`.policy`): cheapest-capable, fastest,
  highest-quality, weighted-blend, all wrapped in capability filtering so an
  incapable backend is never chosen.
* **Hedged / racing requests**: fire the top-``k`` ranked backends concurrently,
  take the first *success*, cancel the losers — guarded by a budget so hedging is
  suppressed when scarce video-seconds are low (a hedge spends twice).
* **Sticky routing** (:mod:`.sticky`): a shot family prefers the backend that
  first served it, for visual continuity — a preference, never an override.
* **Per-provider concurrency + token-bucket rate limits** (:mod:`.concurrency`):
  each backend renders inside its own throughput gate.
* **Automatic failover**: retryable faults / timeouts advance to the next healthy,
  capable backend; a non-retryable fault (bad spec) short-circuits.
* **Full structured-log + metrics** (:mod:`.metrics`): every routing decision is
  logged and tallied.

**The spend gate is sacred.** ``LiveVideoDisabled`` is propagated unchanged, never
recorded as a health failure, and never triggers failover — exactly as the round-1
router guarantees. A :class:`~app.providers.minimax.MiniMaxBudgetExceeded` (a hard
USD refusal) is non-retryable and surfaced immediately too.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.providers.errors import LiveVideoDisabled, ProviderError
from app.providers.types import VideoResult, WanSpec
from app.providers.video_router import VideoBackend

from .capabilities import ProfileBook, ProviderProfile, normalize_profiles
from .concurrency import GateBook, GateConfig
from .health import Clock, HealthConfig, ProviderHealth, classify_error
from .metrics import RouteDecision, RouterMetrics, emit_decision
from .policy import (
    CapabilityFilteredPolicy,
    HealthView,
    RouteContext,
    SelectionPolicy,
    WeightedBlendPolicy,
)
from .sticky import StickyStore, apply_stickiness, family_key


class _NoCapableBackendError(ProviderError):
    """No routable backend can render the requested mode (non-retryable)."""

    retryable = False


@dataclass(frozen=True, slots=True)
class RouterV2Policy:
    """Tunables for :class:`RoutingVideoRouter` (deterministic; no env reads).

    Attributes:
        selection: The :class:`SelectionPolicy` ranking routable backends. Defaults
            to :class:`WeightedBlendPolicy`. Always wrapped in capability filtering.
        hedge: Number of backends to fire concurrently (1 = pure failover, no
            hedge). A hedge spends N× the video-seconds, so keep it small.
        hedge_when_budget_low: When False (default), hedging collapses to 1 attempt
            (failover only) whenever ``budget_low`` is set — never burn double
            video-budget under pressure.
        sticky: Enable shot-family stickiness for visual continuity.
        max_failover_attempts: Cap on sequential backends tried in the failover
            lane (0 = try every routable candidate).
        health: Per-backend health/breaker config.
        default_gate: Default per-backend throughput envelope.
    """

    selection: SelectionPolicy | None = None
    hedge: int = 1
    hedge_when_budget_low: bool = False
    sticky: bool = True
    max_failover_attempts: int = 0
    health: HealthConfig | None = None
    default_gate: GateConfig | None = None

    def __post_init__(self) -> None:
        if self.hedge < 1:
            raise ValueError("hedge must be >= 1")
        if self.max_failover_attempts < 0:
            raise ValueError("max_failover_attempts must be >= 0")


class _HealthAdapter:
    """Adapts the router's :class:`ProviderHealth` map to the policy :class:`HealthView`."""

    def __init__(self, health: Mapping[str, ProviderHealth]) -> None:
        self._health = health

    def success_rate(self, name: str) -> float:
        h = self._health.get(name)
        return h.success_rate() if h is not None else 1.0

    def p50_latency_ms(self, name: str) -> float:
        h = self._health.get(name)
        return h.p50_latency_ms() if h is not None else 0.0

    def p95_latency_ms(self, name: str) -> float:
        h = self._health.get(name)
        return h.p95_latency_ms() if h is not None else 0.0


class RoutingVideoRouter:
    """Route a :class:`WanSpec` across N backends with health, policy, hedging.

    Implements the :class:`VideoBackend` contract so the Generator/pipeline use it
    unchanged. Construction takes backends in **priority order** (first = preferred,
    the tie-breaker for every policy).
    """

    def __init__(
        self,
        backends: Sequence[VideoBackend],
        *,
        policy: RouterV2Policy | None = None,
        profiles: Mapping[str, ProviderProfile] | None = None,
        gates: Mapping[str, GateConfig] | None = None,
        clock: Clock = time.monotonic,
        name: str = "video-router-v2",
    ) -> None:
        if not backends:
            raise ValueError("RoutingVideoRouter requires at least one backend")
        self.name = name
        self._policy = policy or RouterV2Policy()
        self._backends: dict[str, VideoBackend] = {}
        self._order: list[str] = []
        for backend in backends:
            if backend.name in self._backends:
                raise ValueError(f"duplicate backend name: {backend.name!r}")
            self._backends[backend.name] = backend
            self._order.append(backend.name)
        self._clock = clock
        health_cfg = self._policy.health or HealthConfig()
        self._health: dict[str, ProviderHealth] = {
            n: ProviderHealth(name=n, config=health_cfg, _clock=clock) for n in self._order
        }
        self._profiles: ProfileBook = normalize_profiles(profiles)
        self._gates = GateBook(
            dict(gates or {}),
            default=self._policy.default_gate or GateConfig(),
            clock=clock,
        )
        base_selection = self._policy.selection or WeightedBlendPolicy()
        # Always capability-filter so a policy can never pick an incapable backend.
        self._selection: SelectionPolicy = CapabilityFilteredPolicy(base_selection)
        self._health_view: HealthView = _HealthAdapter(self._health)
        self._sticky = StickyStore() if self._policy.sticky else None
        self.metrics = RouterMetrics()

    # -- introspection ---------------------------------------------------- #

    def health(self, name: str) -> ProviderHealth:
        """The :class:`ProviderHealth` record for backend ``name``."""
        return self._health[name]

    def available_names(self) -> list[str]:
        """Backend names whose breaker currently permits a call (priority order)."""
        return [n for n in self._order if self._health[n].available()]

    def health_snapshot(self) -> dict[str, object]:
        """A JSON-friendly snapshot of every backend's health + router metrics."""
        return {
            "router": self.name,
            "backends": [self._health[n].snapshot().as_dict() for n in self._order],
            "metrics": self.metrics.as_dict(),
        }

    async def healthy(self) -> bool:
        """True when *any* breaker-available backend reports healthy."""
        for name in self.available_names():
            try:
                if await self._backends[name].healthy():
                    return True
            except ProviderError:
                continue
        return False

    # -- render ----------------------------------------------------------- #

    async def render(self, spec: WanSpec, *, budget_low: bool = False) -> VideoResult:
        """Render ``spec`` per the policy. Raises ``LiveVideoDisabled`` when gated.

        ``budget_low`` shifts cost-sensitive policies toward cheaper backends and
        (by default) disables hedging so a budget-constrained render never spends
        double video-seconds. The plain ``VideoBackend`` call ``render(spec)`` keeps
        working — ``budget_low`` is an optional router extra.
        """
        ranked, decision, pinned_key = self._plan(spec, budget_low=budget_low)
        if not ranked:
            # No routable, capable backend at all.
            decision.outcome = "no_capable"
            self.metrics.record(decision)
            emit_decision(decision)
            raise _NoCapableBackendError(
                f"no routable backend can render mode {spec.mode.value!r}",
            )

        hedge = self._effective_hedge(budget_low)
        try:
            if hedge > 1 and len(ranked) > 1:
                result = await self._render_hedged(spec, ranked, hedge, decision)
            else:
                result = await self._render_failover(spec, ranked, decision)
        except LiveVideoDisabled:
            # The spend gate fired on the chosen backend(s): propagate unchanged,
            # never a health fault, never a failover.
            decision.outcome = "gate"
            self.metrics.record(decision)
            emit_decision(decision)
            raise
        except ProviderError as exc:
            decision.outcome = "error"
            decision.error_class = type(exc).__name__
            self.metrics.record(decision)
            emit_decision(decision)
            raise

        decision.outcome = "success"
        decision.winner = result.model if decision.winner is None else decision.winner
        if self._sticky is not None and pinned_key is not None and decision.winner is not None:
            self._sticky.set(pinned_key, decision.winner)
        self.metrics.record(decision)
        emit_decision(decision)
        return result

    # -- planning --------------------------------------------------------- #

    def _plan(
        self, spec: WanSpec, *, budget_low: bool
    ) -> tuple[list[str], RouteDecision, str | None]:
        """Rank the routable, capable backends for this render (pure-ish)."""
        available = self.available_names()
        # Record breaker rejections for backends the policy can't see (skipped).
        for name in self._order:
            if name not in available:
                self._health[name].record_rejection()
        candidates = tuple(available) if available else (self._order[0],)
        ctx = RouteContext(
            candidates=candidates,
            profiles=self._profiles,
            health=self._health_view,
            mode=spec.mode,
            budget_low=budget_low,
        )
        ranked = self._selection.rank(ctx)
        decision = RouteDecision(
            mode=spec.mode.value,
            policy=self._selection.name,
            dispatch="hedge" if self._effective_hedge(budget_low) > 1 else "failover",
            candidates=list(candidates),
            ranked=list(ranked),
            budget_low=budget_low,
        )
        pinned_key: str | None = None
        if self._sticky is not None:
            pinned_key = family_key(spec)
            pinned = self._sticky.get(pinned_key)
            if pinned is not None:
                decision.sticky_pinned = pinned
                if pinned in ranked:
                    decision.sticky_hit = True
                    ranked = apply_stickiness(ranked, pinned)
                    decision.ranked = list(ranked)
        return ranked, decision, pinned_key

    def _effective_hedge(self, budget_low: bool) -> int:
        """The hedge fan-out after the budget guard (1 = no hedge)."""
        if budget_low and not self._policy.hedge_when_budget_low:
            return 1
        return self._policy.hedge

    # -- failover lane ---------------------------------------------------- #

    async def _render_failover(
        self, spec: WanSpec, ranked: list[str], decision: RouteDecision
    ) -> VideoResult:
        cap = self._policy.max_failover_attempts or len(ranked)
        last_error: ProviderError | None = None
        for name in ranked[:cap]:
            decision.attempts += 1
            self.metrics.record_attempt(name)
            try:
                result = await self._attempt(name, spec)
            except LiveVideoDisabled:
                # Gate is not a fault: do not record health, do not failover.
                raise
            except ProviderError as exc:
                self._health[name].record_failure(classify_error(exc))
                last_error = exc
                if not exc.retryable:
                    # A non-retryable fault (bad spec / hard budget) fails the same
                    # everywhere — surface it now instead of trying the next backend.
                    raise
                continue
            decision.winner = name
            return result
        assert last_error is not None  # at least one candidate always ran
        raise last_error

    # -- hedge / race lane ------------------------------------------------ #

    async def _render_hedged(
        self, spec: WanSpec, ranked: list[str], hedge: int, decision: RouteDecision
    ) -> VideoResult:
        """Fire the top-``hedge`` ranked backends concurrently; first success wins.

        Losers are cancelled the instant a success lands. A non-retryable fault (or
        the spend gate) on any racer aborts the field and propagates immediately —
        retrying a bad spec on a sibling would only burn the same fault. If every
        started racer fails *retryably*, fall back to the failover lane over the
        remaining (un-raced) ranked backends so a hedge never strands a render.
        """
        racers = ranked[:hedge]
        decision.hedges_launched = max(0, len(racers) - 1)
        for name in racers:
            decision.attempts += 1
            self.metrics.record_attempt(name)

        tasks: dict[asyncio.Task[VideoResult], str] = {
            asyncio.ensure_future(self._attempt(name, spec)): name for name in racers
        }
        pending = set(tasks)
        last_error: ProviderError | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    name = tasks[task]
                    try:
                        result = task.result()
                    except LiveVideoDisabled:
                        await self._cancel_all(pending)
                        raise
                    except ProviderError as exc:
                        self._health[name].record_failure(classify_error(exc))
                        last_error = exc
                        if not exc.retryable:
                            await self._cancel_all(pending)
                            raise
                        continue
                    decision.winner = name
                    await self._cancel_all(pending)
                    return result
        finally:
            await self._cancel_all(pending)

        # Every racer failed retryably — fall back to any ranked backends we did not
        # race (sequential failover) so the render still gets its remaining chances.
        remainder = ranked[hedge:]
        if remainder:
            return await self._render_failover(spec, remainder, decision)
        assert last_error is not None
        raise last_error

    # -- one attempt through a backend's throughput gate ------------------ #

    async def _attempt(self, name: str, spec: WanSpec) -> VideoResult:
        """Render through backend ``name`` inside its gate, stamping latency."""
        backend = self._backends[name]
        gate = self._gates.gate(name)
        async with gate.slot():
            started = self._clock()
            result = await backend.render(spec)
            latency_ms = (self._clock() - started) * 1000.0
            self._health[name].record_success(latency_ms=latency_ms)
            return result

    @staticmethod
    async def _cancel_all(tasks: set[asyncio.Task[VideoResult]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, ProviderError, Exception):
                await task


__all__ = [
    "RouterV2Policy",
    "RoutingVideoRouter",
]
