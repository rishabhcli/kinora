"""Target-tracking + predictive worker autoscaler (kinora.md §4.6, §4.9, §12.2).

The controller maps a :class:`~app.autoscale.signal.DemandSnapshot` to a
:class:`ScalingPlan` — a desired replica count per :class:`~app.autoscale.lanes.Lane`
with a human-readable rationale — that an actuator applies. It is the policy brain;
it owns the time-based anti-flap state and the cost cap. Pure given its clock:
feed the same snapshots with the same :class:`~app.autoscale.clock.VirtualClock`
advances and you get the same plan sequence.

Control law (per lane, every tick):

1. **Target-tracking** — size to the effective backlog:
   ``ceil(effective_backlog / jobs_per_worker)`` (§4.9). Backlog already folds in
   the predictive look-ahead demand from reader velocity (the pre-warm term), so a
   velocity spike raises the target *before* the queue fills.
2. **Predictive boost** — additionally lift the committed-serving lane by the
   aggregate underrun risk so capacity warms ahead of a near-dry buffer (§4.6/§4.10).
3. **Provider dampening** — if the provider lane is already at its quota
   (``provider_saturation -> 1``), suppress further scale-out: more workers would
   just 429 (the §-noted image/video rate quota), so we hold.
4. **Bounds** — clamp to ``[min, max]`` and apply the lane's ramp step
   (``scale_out_step``) so a spike doesn't over-provision in one jump.
5. **Hysteresis** — only act when the target leaves a dead-band around the current
   size; small jitter is ignored (no flapping on a one-job wobble).
6. **Asymmetric cooldown** — scale-**out** is allowed immediately (a stalling buffer
   is urgent); scale-**in** must wait out ``scale_in_cooldown_s`` *and* respects the
   replica's ``warmup_s`` (don't tear down a GPU that just warmed). Scale-in also
   removes at most ``scale_in_step`` replicas — graceful drain.
7. **Cost cap** — the summed plan cost must fit ``max_cost``. If it doesn't, trim
   the *cheapest-value* lanes first (speculative/keyframe capacity before the
   committed buffer's lane) so the reader-facing buffer is protected last.

Spend safety: this controller scales *workers*, never video. It cannot enable
``KINORA_LIVE_VIDEO`` nor spend a credit; it only decides how many drainers exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.autoscale.clock import Clock, MonotonicClock
from app.autoscale.lanes import Lane, LanePool
from app.autoscale.signal import DemandSnapshot, LanePressure
from app.core.logging import get_logger

logger = get_logger("app.autoscale.controller")

__all__ = [
    "AutoscalerConfig",
    "LaneDecision",
    "RenderAutoscaler",
    "ScalingPlan",
]


@dataclass(frozen=True, slots=True)
class AutoscalerConfig:
    """Tunables governing the whole controller (env-backed via Settings).

    Attributes:
        scale_in_cooldown_s: min seconds between a scale event and a later scale-in
            on the same lane (asymmetric anti-flap; scale-out has no cooldown).
        hysteresis_band: fractional dead-band around the current size; a scale-out
            target must exceed ``cur * (1 + band)`` (or ``cur + hysteresis_floor``,
            whichever is larger) to fire. Absorbs boundary jitter without wedging
            scale-in (scale-in is governed by the cooldown, not the band).
        hysteresis_floor: absolute minimum scale-out margin (replicas). Stops a
            one-job wobble near the floor from triggering immediate growth even when
            the *fractional* band rounds to zero on a tiny pool. Default 1.
        predictive_gain: how strongly aggregate underrun risk lifts the committed
            lane target (replicas per unit risk). 0 disables pre-warm.
        max_cost: ceiling on summed ``cost_per_replica`` across the plan; ``inf``
            disables the cap.
        latency_slo_s / underrun_horizon_s / seconds_per_shot: passed through to
            :meth:`DemandSnapshot.lane_pressures`.
    """

    scale_in_cooldown_s: float = 60.0
    hysteresis_band: float = 0.15
    hysteresis_floor: float = 1.0
    predictive_gain: float = 1.0
    max_cost: float = float("inf")
    latency_slo_s: float = 25.0
    underrun_horizon_s: float = 30.0
    seconds_per_shot: float = 5.0

    def __post_init__(self) -> None:
        if self.scale_in_cooldown_s < 0:
            raise ValueError("scale_in_cooldown_s must be >= 0")
        if not 0.0 <= self.hysteresis_band < 1.0:
            raise ValueError("hysteresis_band must be in [0, 1)")
        if self.hysteresis_floor < 0:
            raise ValueError("hysteresis_floor must be >= 0")
        if self.predictive_gain < 0:
            raise ValueError("predictive_gain must be >= 0")
        if self.max_cost < 0:
            raise ValueError("max_cost must be >= 0")


class LaneDecision(BaseModel):
    """The per-lane outcome inside a :class:`ScalingPlan`."""

    lane: Lane
    current: int = Field(ge=0)
    target: int = Field(ge=0)
    desired: int = Field(ge=0)
    delta: int
    pressure: float
    reason: str
    cost_trimmed: int = 0


class ScalingPlan(BaseModel):
    """Desired replica count per lane plus rationale — the actuator's instruction."""

    decisions: dict[Lane, LaneDecision]
    total_cost: float
    cost_capped: bool = False

    @property
    def desired(self) -> dict[Lane, int]:
        return {lane: d.desired for lane, d in self.decisions.items()}

    @property
    def changed(self) -> bool:
        return any(d.delta != 0 for d in self.decisions.values())

    def rationale(self) -> str:
        """One-line human summary of every lane that moved (or held)."""
        parts = [
            f"{lane.value}:{d.current}->{d.desired} ({d.reason})"
            for lane, d in self.decisions.items()
        ]
        return "; ".join(parts)


@dataclass
class _LaneState:
    """Mutable per-lane bookkeeping the controller carries between ticks."""

    current: int
    last_scale_at: float = float("-inf")
    last_scale_out_at: float = float("-inf")


@dataclass
class RenderAutoscaler:
    """Stateful target-tracking + predictive controller over the lane pools.

    Holds the current sizes and per-lane scale timestamps. :meth:`plan` is the only
    entry point; it never mutates the snapshot and only advances its own state.
    """

    pools: dict[Lane, LanePool]
    config: AutoscalerConfig = field(default_factory=AutoscalerConfig)
    clock: Clock = field(default_factory=MonotonicClock)
    _state: dict[Lane, _LaneState] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for lane, pool in self.pools.items():
            self._state.setdefault(lane, _LaneState(current=pool.min_replicas))

    # --- introspection --------------------------------------------------

    @property
    def current(self) -> dict[Lane, int]:
        return {lane: st.current for lane, st in self._state.items()}

    def total_cost(self) -> float:
        return sum(self.pools[lane].cost_at(st.current) for lane, st in self._state.items())

    # --- the control law ------------------------------------------------

    def plan(self, snapshot: DemandSnapshot) -> ScalingPlan:
        """Compute the next :class:`ScalingPlan` from a demand observation."""
        now = self.clock.now()
        lanes = list(self.pools.keys())
        pressures = snapshot.lane_pressures(
            lanes,
            latency_slo_s=self.config.latency_slo_s,
            underrun_horizon_s=self.config.underrun_horizon_s,
            seconds_per_shot=self.config.seconds_per_shot,
        )
        underrun_total = snapshot.total_underrun_risk()

        decisions: dict[Lane, LaneDecision] = {}
        for lane in lanes:
            decisions[lane] = self._plan_lane(
                lane=lane,
                pool=self.pools[lane],
                pressure=pressures[lane],
                underrun_total=underrun_total,
                now=now,
            )

        plan = self._apply_cost_cap(decisions)
        # Commit the chosen sizes + scale timestamps.
        for lane, d in plan.decisions.items():
            st = self._state[lane]
            if d.desired != st.current:
                st.last_scale_at = now
                if d.desired > st.current:
                    st.last_scale_out_at = now
                st.current = d.desired
        if plan.changed:
            logger.info(
                "autoscale.plan",
                rationale=plan.rationale(),
                total_cost=round(plan.total_cost, 2),
                cost_capped=plan.cost_capped,
            )
        return plan

    def _plan_lane(
        self,
        *,
        lane: Lane,
        pool: LanePool,
        pressure: LanePressure,
        underrun_total: float,
        now: float,
    ) -> LaneDecision:
        st = self._state[lane]
        cur = st.current

        # (1) target-tracking on the effective backlog (already includes look-ahead).
        base_target = pool.target_for_backlog(pressure.effective_backlog)
        target = base_target

        # (2) predictive pre-warm: add headroom to the committed-serving lane
        # proportional to aggregate underrun risk. Additive (not a floor) so the gain
        # knob always buys extra warm capacity above what backlog-tracking alone sized
        # — the controller leans ahead of a near-dry buffer (§4.6/§4.10).
        if pressure.underrun_pressure > 0 and self.config.predictive_gain > 0:
            prewarm = int(round(underrun_total * self.config.predictive_gain))
            target = pool.clamp(float(base_target + prewarm))

        # also honour latency saturation: a hot pool needs at least one more worker.
        if pressure.latency_pressure >= 1.0:
            target = pool.clamp(float(max(target, cur + 1)))

        reason = "steady"
        desired = cur

        # (5) hysteresis is a *scale-out* guard only (asymmetric anti-flap): a
        # scale-out fires only when the target clears both the fractional band and a
        # small absolute floor, so boundary jitter (a one-job wobble that bumps the
        # rounded target by 1) never triggers growth. Scale-IN is deliberately NOT
        # band-gated — it is governed by the cooldown + warm-up below — so a lane
        # that legitimately drained (or a GPU that scales 1-at-a-time) is never wedged.
        scale_out_margin = max(cur * self.config.hysteresis_band, self.config.hysteresis_floor)

        if target > cur:
            if (target - cur) <= scale_out_margin:
                reason = "hold:hysteresis"
            elif lane == Lane.PROVIDER and pressure.provider_saturation >= 0.999:
                # (3) provider dampening: at quota, adding workers only 429s — hold.
                reason = "hold:provider-quota"
            else:
                # (4) ramp by scale_out_step (0 = jump straight to target).
                if pool.scale_out_step > 0:
                    desired = pool.clamp(float(min(target, cur + pool.scale_out_step)))
                else:
                    desired = target
                reason = "scale-out"
        elif target < cur:
            # (6) scale-in cooldown + warm-up guard + graceful drain step.
            cooled = (now - st.last_scale_at) >= self.config.scale_in_cooldown_s
            warmed = (now - st.last_scale_out_at) >= pool.warmup_s
            if cooled and warmed:
                step_floor = max(target, cur - pool.scale_in_step)
                desired = pool.clamp(float(step_floor))
                reason = "scale-in"
            else:
                reason = "hold:cooldown" if not cooled else "hold:warmup"

        return LaneDecision(
            lane=lane,
            current=cur,
            target=target,
            desired=desired,
            delta=desired - cur,
            pressure=round(pressure.pressure, 4),
            reason=reason,
        )

    def _apply_cost_cap(self, decisions: dict[Lane, LaneDecision]) -> ScalingPlan:
        """(7) Trim the plan to ``max_cost``, sacrificing cheapest-value lanes first.

        Trim order protects the reader-facing buffer: GPU (most expensive) and the
        speculative/keyframe-serving capacity are shed before the committed lane's
        minimum. We never trim below a lane's ``min_replicas``.
        """
        cap = self.config.max_cost

        def plan_cost(ds: dict[Lane, LaneDecision]) -> float:
            return sum(self.pools[lane].cost_at(d.desired) for lane, d in ds.items())

        total = plan_cost(decisions)
        if total <= cap:
            return ScalingPlan(decisions=decisions, total_cost=total, cost_capped=False)

        # Trim most-expensive-per-replica lanes down toward their minimum until we fit.
        # Stable order: highest cost_per_replica first, then lane name for determinism.
        trim_order = sorted(
            decisions.keys(),
            key=lambda ln: (-self.pools[ln].cost_per_replica, ln.value),
        )
        capped = dict(decisions)
        for lane in trim_order:
            if plan_cost(capped) <= cap:
                break
            pool = self.pools[lane]
            d = capped[lane]
            floor = pool.min_replicas
            while d.desired > floor and plan_cost(capped) > cap:
                new_desired = d.desired - 1
                d = d.model_copy(
                    update={
                        "desired": new_desired,
                        "delta": new_desired - d.current,
                        "cost_trimmed": d.cost_trimmed + 1,
                        "reason": (
                            d.reason if d.reason.startswith("cost-cap") else "cost-cap:" + d.reason
                        ),
                    }
                )
                capped[lane] = d
        return ScalingPlan(decisions=capped, total_cost=plan_cost(capped), cost_capped=True)
