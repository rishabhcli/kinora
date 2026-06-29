"""The predictive autoscaler for the inference fleet (kinora.md §4.5, §4.9, §12.2).

This is the elasticity brain. Where the render-lane autoscaler
(:mod:`app.queue.autoscale`) sizes the *render priority lanes* reactively from
queue depth, this sizes a **heterogeneous GPU backend** from a *forecast* of the
next cold-start window's demand — the thing you must anticipate, because a cold
start is paid in wall-clock the reader experiences as a stall (§instances).

The controller is a pure function from *(forecast, current pool state)* to a
**desired warm-worker count**, wrapped in a stateful guard that enforces the four
production behaviours an inference autoscaler must have:

1. **Forecast lookahead.** Size to the demand expected ``cold_start_s`` from now,
   at a configurable quantile (p95 headroom), so capacity arrives *before* the
   load does — not one cold-start late. Sizing reuses the queue-theory
   :func:`~app.inference.scaling.queueing.servers_for_tail_target` so the count
   meets the latency SLO, not just the throughput.
2. **Scale-to-zero.** When the forecast is ~zero *and* the pool has been idle
   past ``scale_to_zero_idle_s``, the desired count collapses to zero — you stop
   paying for a fleet nobody is reading against (§4.7 idle-pause economics). A
   non-zero ``warm_pool`` floor keeps a hot standby to hide the next cold start.
3. **Warm-pool floor.** ``warm_pool`` workers are always kept hot above the
   scale-to-zero floor, so the first request after a lull doesn't eat a full cold
   start. Set ``warm_pool=0`` for pure scale-to-zero (cheapest, slowest first hit).
4. **Anti-flap.** Scale-up is immediate (a stalling buffer is urgent); scale-down
   waits out an asymmetric ``scale_down_cooldown_s`` *and* a down-step damper, so
   a noisy forecast can't oscillate the fleet. A configurable ``max_step`` rate-
   limits how fast the pool grows/shrinks per tick.

The output is a :class:`ScaleDecision` (a *desired* count + rationale), never a
side effect — the orchestrator (an ECS desired-count, the pool simulator, a
supervisor) owns the actual launch/drain. That keeps the brain deterministic and
unit-testable against an injected clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger
from app.inference.scaling.contracts import BackendDescriptor
from app.inference.scaling.forecast import Forecast
from app.inference.scaling.queueing import servers_for_tail_target

logger = get_logger("app.inference.scaling.autoscaler")

__all__ = [
    "ScalingPolicy",
    "ScaleAction",
    "ScaleDecision",
    "PredictiveAutoscaler",
]


class ScaleAction(StrEnum):
    """The qualitative move a decision represents (for logging/inspection)."""

    UP = "up"
    DOWN = "down"
    HOLD = "hold"
    TO_ZERO = "to_zero"
    FROM_ZERO = "from_zero"


@dataclass(frozen=True, slots=True)
class ScalingPolicy:
    """Bounds + behaviour knobs for one backend's autoscaler.

    ``warm_pool`` is the hot-standby floor kept above scale-to-zero; ``min_workers``
    is the floor when *not* scaled to zero (often equal to ``warm_pool``).
    ``headroom_quantile`` sizes to that quantile of forecast demand (p95 default).
    ``target_tail_s`` / ``tail_quantile`` define the latency SLO the queue-theory
    sizing must meet. ``scale_to_zero`` enables collapsing to zero on a sustained
    idle; ``scale_to_zero_idle_s`` is how long demand must stay ~zero first.
    """

    min_workers: int = 0
    max_workers: int = 16
    warm_pool: int = 1
    headroom_quantile: float = 0.95
    target_tail_s: float = 60.0
    tail_quantile: float = 0.95
    scale_to_zero: bool = True
    scale_to_zero_idle_s: float = 120.0
    scale_up_cooldown_s: float = 0.0  # up is usually immediate
    scale_down_cooldown_s: float = 90.0
    #: Max change in warm-worker count applied in a single tick (rate limit).
    max_step: int = 4
    #: Demand at/below this (req/s) counts as "idle" for scale-to-zero.
    idle_threshold_rps: float = 1e-6

    def __post_init__(self) -> None:
        if self.min_workers < 0 or self.max_workers < self.min_workers:
            raise ValueError("require 0 <= min_workers <= max_workers")
        if not 0 <= self.warm_pool <= self.max_workers:
            raise ValueError("require 0 <= warm_pool <= max_workers")
        if not 0.0 < self.headroom_quantile < 1.0:
            raise ValueError("headroom_quantile must be in (0, 1)")
        if not 0.0 < self.tail_quantile < 1.0:
            raise ValueError("tail_quantile must be in (0, 1)")
        if self.target_tail_s <= 0.0:
            raise ValueError("target_tail_s must be positive")
        if self.max_step < 1:
            raise ValueError("max_step must be >= 1")
        if self.scale_down_cooldown_s < 0.0 or self.scale_up_cooldown_s < 0.0:
            raise ValueError("cooldowns must be non-negative")

    @property
    def idle_floor(self) -> int:
        """The smallest pool the policy ever holds when there *is* demand."""
        return max(self.min_workers, self.warm_pool)


@dataclass(frozen=True, slots=True)
class ScaleDecision:
    """The autoscaler's verdict for one backend at one tick.

    ``desired`` is the warm-worker count the orchestrator should converge to;
    ``raw_target`` is the unclamped queue-theory size (before warm-pool/step/anti-
    flap), kept for the capacity report. ``action`` is the qualitative move.
    """

    backend_id: str
    desired: int
    raw_target: int
    previous: int
    action: ScaleAction
    reason: str
    forecast_point: float
    headroom_demand: float

    @property
    def delta(self) -> int:
        """Signed change from the previous count."""
        return self.desired - self.previous

    @property
    def changed(self) -> bool:
        return self.delta != 0

    def to_dict(self) -> dict[str, object]:
        """JSON projection for the capacity report / logs."""
        return {
            "backend_id": self.backend_id,
            "desired": self.desired,
            "raw_target": self.raw_target,
            "previous": self.previous,
            "delta": self.delta,
            "action": self.action.value,
            "reason": self.reason,
            "forecast_point": round(self.forecast_point, 5),
            "headroom_demand": round(self.headroom_demand, 5),
        }


def _raw_target(
    *, descriptor: BackendDescriptor, demand_rps: float, policy: ScalingPolicy
) -> int:
    """Queue-theory size for ``demand_rps`` against the latency SLO.

    Converts the per-worker concurrency into the M/M/c server count: the
    queue-theory sizer returns request-server slots; one warm worker provides
    ``descriptor.concurrency`` slots, so we divide and round up.
    """
    if demand_rps <= 0.0:
        return 0
    service = descriptor.service_time_s
    # The tail target must clear the irreducible service time; if the policy's
    # target is below it (mis-configured), fall back to a utilisation-style size.
    target = max(policy.target_tail_s, service * 1.01)
    cap_slots = max(1, policy.max_workers * descriptor.concurrency)
    try:
        slots = servers_for_tail_target(
            arrival_rate_per_s=demand_rps,
            service_time_s=service,
            target_response_s=target,
            quantile=policy.tail_quantile,
            max_servers=cap_slots,
        )
    except ValueError:
        # Demand exceeds what the fleet cap can serve at the SLO: saturate at the
        # cap (the autoscaler does what it can; load-shedding/preemption handle the
        # overflow). The orchestrator clamp below pins it to max_workers anyway.
        slots = cap_slots
    # Slots → warm workers (each worker serves `concurrency` slots).
    workers = -(-slots // descriptor.concurrency)  # ceil division
    return workers


@dataclass
class PredictiveAutoscaler:
    """Stateful per-backend autoscaler: forecast → desired warm-worker count.

    One instance owns one backend's scaling state (current count, last-scale +
    idle timestamps). ``decide`` is called on every control tick with the current
    demand forecast and the observed warm count; it returns a :class:`ScaleDecision`.
    Pass an explicit ``clock`` (monotonic seconds) so tests drive cooldowns and the
    idle timer deterministically.
    """

    descriptor: BackendDescriptor
    policy: ScalingPolicy
    current: int = 0
    clock: Any = None
    _last_scale_at: float = field(default=float("-inf"))
    _idle_since: float | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.monotonic
        if self.current == 0:
            self.current = self.policy.min_workers

    # ------------------------------------------------------------------ #
    # The control tick
    # ------------------------------------------------------------------ #

    def decide(self, *, forecast: Forecast, observed_warm: int | None = None) -> ScaleDecision:
        """Compute the desired warm count for this tick from a demand forecast.

        ``forecast`` is the demand (req/s) expected one cold-start ahead;
        ``observed_warm`` (if given) reconciles ``self.current`` with what the
        orchestrator actually has warm right now (drift correction).
        """
        now = self.clock()
        if observed_warm is not None:
            self.current = observed_warm

        headroom = forecast.quantile(self.policy.headroom_quantile)
        raw = _raw_target(
            descriptor=self.descriptor, demand_rps=headroom, policy=self.policy
        )

        idle = headroom <= self.policy.idle_threshold_rps
        self._track_idle(now, idle=idle)

        target, action, reason = self._target_with_floor(now=now, raw=raw, idle=idle)
        desired = self._apply_anti_flap(now=now, target=target, action=action)

        decision = ScaleDecision(
            backend_id=self.descriptor.backend_id,
            desired=desired,
            raw_target=raw,
            previous=self.current,
            action=action if desired != self.current else ScaleAction.HOLD,
            reason=reason,
            forecast_point=forecast.point,
            headroom_demand=headroom,
        )
        if decision.changed:
            self._last_scale_at = now
            logger.info("autoscaler.scale", **decision.to_dict())
        self.current = desired
        return decision

    # ------------------------------------------------------------------ #
    # Floor / scale-to-zero
    # ------------------------------------------------------------------ #

    def _track_idle(self, now: float, *, idle: bool) -> None:
        if idle:
            if self._idle_since is None:
                self._idle_since = now
        else:
            self._idle_since = None

    def _idle_long_enough(self, now: float) -> bool:
        if self._idle_since is None:
            return False
        return (now - self._idle_since) >= self.policy.scale_to_zero_idle_s

    def _target_with_floor(
        self, *, now: float, raw: int, idle: bool
    ) -> tuple[int, ScaleAction, str]:
        """Apply the warm-pool floor and scale-to-zero collapse to a raw target."""
        if idle:
            if self.policy.scale_to_zero and self._idle_long_enough(now):
                return 0, ScaleAction.TO_ZERO, "sustained idle: scaled to zero"
            if self.policy.scale_to_zero:
                # Idle but the scale-to-zero window hasn't elapsed: hold the
                # capacity we have (never drop below the floor) so a reader who
                # returns mid-window is served warm — the collapse is a single,
                # deliberate TO_ZERO step once the window passes, not a slow bleed.
                hold = max(self.current, self.policy.idle_floor)
                return hold, ScaleAction.HOLD, "idle: holding capacity pre-scale-to-zero"
            # Scale-to-zero disabled: settle on the warm-pool floor (scaling in
            # toward it if we're above it, subject to the down cooldown).
            floor = self.policy.idle_floor
            if floor < self.current:
                return floor, ScaleAction.DOWN, "idle: scaling in to warm-pool floor"
            return floor, ScaleAction.HOLD, "idle: holding warm-pool floor"

        # There is demand. Size to max(queue-theory, warm-pool floor).
        target = max(raw, self.policy.idle_floor)
        target = min(target, self.policy.max_workers)
        if self.current == 0 and target > 0:
            return target, ScaleAction.FROM_ZERO, "demand returned: warming from zero"
        if target > self.current:
            return target, ScaleAction.UP, "forecast demand up: scaling out"
        if target < self.current:
            return target, ScaleAction.DOWN, "forecast demand down: scaling in"
        return target, ScaleAction.HOLD, "forecast steady: holding"

    # ------------------------------------------------------------------ #
    # Anti-flap (asymmetric cooldown + step limiting)
    # ------------------------------------------------------------------ #

    def _apply_anti_flap(self, *, now: float, target: int, action: ScaleAction) -> int:
        """Rate-limit + cooldown the move toward ``target`` (availability bias)."""
        cur = self.current
        if target == cur:
            return cur

        scaling_up = target > cur
        # Scale-to-zero and from-zero are urgent moves; they bypass the down
        # cooldown so an idle fleet collapses promptly and a returning reader is
        # served promptly. Ordinary down-moves wait out the cooldown.
        urgent = action in (ScaleAction.TO_ZERO, ScaleAction.FROM_ZERO)
        if not scaling_up and not urgent:
            since = now - self._last_scale_at
            if since < self.policy.scale_down_cooldown_s:
                return cur  # within cooldown: hold (don't undo a fresh burst)
        elif scaling_up and not urgent:
            since = now - self._last_scale_at
            if since < self.policy.scale_up_cooldown_s:
                return cur

        # Step-limit the move (don't grow/shrink the whole fleet in one tick),
        # except a to-zero collapse which is allowed in full.
        if action is ScaleAction.TO_ZERO:
            return 0
        step = self.policy.max_step
        if scaling_up:
            return min(target, cur + step)
        return max(target, cur - step)
