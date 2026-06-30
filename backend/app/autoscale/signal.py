"""Demand-signal model for the render autoscaler (kinora.md §4.5–§4.10, §12.2).

The controller cannot scale on a single number. Render demand is multi-dimensional
and the dimensions have *different time constants*:

* **Queue depth by QoS class** — the instantaneous backlog. Committed depth is the
  urgent one (a hungry reader is waiting); speculative depth is droppable.
* **Buffer-underrun risk** — the *predictive* term. A reader at velocity ``v`` with
  only ``committed_seconds_ahead`` of film buffered will run dry in
  ``ahead / v_video`` seconds; aggregated across active sessions this is the
  pressure that should *pre-warm* capacity before the queue even fills (§4.6/§4.10).
* **In-flight provider jobs** — work already dispatched to Wan/MiniMax that hasn't
  returned. High in-flight against the provider quota means adding workers won't
  help (they'd just 429), so this *dampens* provider scale-out.
* **p95 render latency** — the realised service time. Rising p95 means the current
  pool is saturated even if depth looks flat (jobs are slow, not absent); it is the
  classic target-tracking control variable.

This module turns raw observations into a normalised, per-lane **pressure** in
``[0, ~]`` plus an **effective backlog** (depth + look-ahead demand) that the
controller's target-tracking term consumes. Everything is a pure function of an
immutable :class:`DemandSnapshot`; no clock, no Redis, no provider calls. The
controller owns time; the signal owns *interpretation*.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.autoscale.lanes import Lane, QoSClass, lane_for_qos

__all__ = [
    "DEFAULT_VIDEO_SECONDS_PER_SHOT",
    "DemandSnapshot",
    "LanePressure",
    "SessionDemand",
    "percentile",
]

#: A promoted shot yields ~5s of finished film (§4.10 worked example).
DEFAULT_VIDEO_SECONDS_PER_SHOT = 5.0
#: Low watermark (§4.5): below this many seconds-ahead a session is at-risk.
DEFAULT_LOW_WATERMARK_S = 25.0
#: How urgent an underrun must be (seconds-to-dry) to count as a pre-warm trigger.
DEFAULT_UNDERRUN_HORIZON_S = 30.0
#: p95 latency (s) at/above which the lane is considered saturated (pressure 1.0).
DEFAULT_LATENCY_SLO_S = 25.0


def percentile(values: Sequence[float], q: float) -> float:
    """Deterministic linear-interpolation percentile (q in [0, 1]).

    Pure and stable so latency pressure is reproducible from a fixed sample. Empty
    input returns 0.0; a single value returns itself.
    """
    if not values:
        return 0.0
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


@dataclass(frozen=True, slots=True)
class SessionDemand:
    """One active reading session's contribution to look-ahead demand (§4.5).

    Attributes:
        velocity_wps: reader velocity (words/second); higher drains buffer faster.
        committed_seconds_ahead: finished film buffered in front of the reader (s).
        words_per_video_second: book density — how many words one second of film
            covers. Used to convert reading velocity into film-consumption rate.
        idle: when True the reader has paused (§4.7); contributes no pressure.
    """

    velocity_wps: float
    committed_seconds_ahead: float
    words_per_video_second: float = 6.0
    idle: bool = False

    def film_consumption_rate(self) -> float:
        """Seconds of *finished film* consumed per wall-second at this velocity."""
        if self.idle or self.words_per_video_second <= 0:
            return 0.0
        return max(0.0, self.velocity_wps / self.words_per_video_second)

    def seconds_to_dry(self) -> float:
        """Wall-seconds until the committed buffer underruns (inf if not draining)."""
        rate = self.film_consumption_rate()
        if rate <= 0:
            return math.inf
        return max(0.0, self.committed_seconds_ahead) / rate

    def underrun_risk(
        self,
        *,
        horizon_s: float = DEFAULT_UNDERRUN_HORIZON_S,
        low_watermark_s: float = DEFAULT_LOW_WATERMARK_S,
    ) -> float:
        """Risk in ``[0, 1]`` that this session runs dry within ``horizon_s``.

        1.0 when the buffer is empty/draining and will dry inside the horizon; 0.0
        when idle or buffered well past the low watermark. Linear between.
        """
        if self.idle:
            return 0.0
        ttl = self.seconds_to_dry()
        if math.isinf(ttl):
            # Not draining; risk is purely how far below the low watermark we sit.
            if self.committed_seconds_ahead >= low_watermark_s:
                return 0.0
            return 1.0 - (self.committed_seconds_ahead / max(low_watermark_s, 1e-9))
        if ttl >= horizon_s:
            return 0.0
        return 1.0 - (ttl / max(horizon_s, 1e-9))

    def shots_needed(
        self,
        *,
        horizon_s: float = DEFAULT_UNDERRUN_HORIZON_S,
        seconds_per_shot: float = DEFAULT_VIDEO_SECONDS_PER_SHOT,
    ) -> float:
        """Committed shots needed to keep this session buffered over ``horizon_s``.

        The forward demand a *predictive* controller should provision for: the film
        the reader will consume in the horizon, minus what is already buffered.
        """
        if self.idle:
            return 0.0
        demand_s = self.film_consumption_rate() * horizon_s - self.committed_seconds_ahead
        if demand_s <= 0:
            return 0.0
        return demand_s / max(seconds_per_shot, 1e-9)


class LanePressure(BaseModel):
    """Interpreted, normalised demand for one lane (controller input).

    ``effective_backlog`` blends the realised queue depth with predictive look-ahead
    demand; ``pressure`` is a 0..1+ saturation score combining depth, latency and
    underrun risk. ``provider_saturation`` (provider lane only) is how close
    in-flight jobs are to the provider quota — it dampens scale-out.
    """

    lane: Lane
    queue_depth: int = Field(ge=0)
    inflight: int = Field(ge=0)
    effective_backlog: float = Field(ge=0.0)
    p95_latency_s: float = Field(ge=0.0)
    latency_pressure: float = Field(ge=0.0)
    underrun_pressure: float = Field(ge=0.0)
    provider_saturation: float = Field(ge=0.0, le=1.0, default=0.0)

    @property
    def pressure(self) -> float:
        """Aggregate saturation: max of latency and a depth/underrun blend."""
        depth_term = self.underrun_pressure
        return max(self.latency_pressure, depth_term)


@dataclass(frozen=True, slots=True)
class DemandSnapshot:
    """An immutable, point-in-time observation of render demand.

    Built from live queue/scheduler/provider reads in production, or from a
    synthetic trace in the simulator. Pure: :meth:`lane_pressures` derives the
    controller inputs without any I/O.

    Attributes:
        depth_by_qos: queued job count per QoS class.
        inflight_by_lane: dispatched-but-unfinished job count per lane (provider
            in-flight is the quota signal).
        latency_samples_s: recent per-lane render durations (for p95).
        sessions: active reading sessions driving look-ahead demand.
        provider_quota: max concurrent provider jobs tolerated (for saturation);
            ``None`` disables the dampener.
    """

    depth_by_qos: Mapping[QoSClass, int] = field(default_factory=dict)
    inflight_by_lane: Mapping[Lane, int] = field(default_factory=dict)
    latency_samples_s: Mapping[Lane, Sequence[float]] = field(default_factory=dict)
    sessions: Sequence[SessionDemand] = field(default_factory=tuple)
    provider_quota: int | None = None

    def total_underrun_risk(self) -> float:
        """Sum of per-session underrun risk across non-idle sessions."""
        return sum(s.underrun_risk() for s in self.sessions)

    def lane_pressures(
        self,
        lanes: Sequence[Lane],
        *,
        latency_slo_s: float = DEFAULT_LATENCY_SLO_S,
        underrun_horizon_s: float = DEFAULT_UNDERRUN_HORIZON_S,
        seconds_per_shot: float = DEFAULT_VIDEO_SECONDS_PER_SHOT,
    ) -> dict[Lane, LanePressure]:
        """Interpret this snapshot into a :class:`LanePressure` per requested lane."""
        # Route QoS depth to physical lanes.
        depth_by_lane: dict[Lane, int] = dict.fromkeys(lanes, 0)
        for qos, depth in self.depth_by_qos.items():
            lane = lane_for_qos(qos)
            if lane in depth_by_lane:
                depth_by_lane[lane] += max(0, depth)

        # Predictive look-ahead demand (committed-style shots), routed to the lane
        # that serves committed video. Underrun risk is global (any session at risk
        # pressures the committed-serving lane).
        committed_lane = lane_for_qos(QoSClass.COMMITTED)
        lookahead_shots = sum(
            s.shots_needed(horizon_s=underrun_horizon_s, seconds_per_shot=seconds_per_shot)
            for s in self.sessions
        )
        underrun_risk_total = self.total_underrun_risk()

        out: dict[Lane, LanePressure] = {}
        for lane in lanes:
            depth = depth_by_lane.get(lane, 0)
            inflight = max(0, self.inflight_by_lane.get(lane, 0))
            samples = list(self.latency_samples_s.get(lane, ()))
            p95 = percentile(samples, 0.95)
            latency_pressure = p95 / latency_slo_s if latency_slo_s > 0 else 0.0

            # Effective backlog = realised depth + predictive look-ahead on the
            # committed-serving lane. Other lanes only see their realised depth.
            eff = float(depth + inflight)
            underrun_pressure = 0.0
            if lane == committed_lane:
                eff += lookahead_shots
                # Normalise risk into a 0..1 pressure: each at-risk session unit
                # contributes; capped softly so one frantic reader doesn't explode it.
                underrun_pressure = min(1.5, underrun_risk_total)

            provider_sat = 0.0
            if self.provider_quota and self.provider_quota > 0 and lane == Lane.PROVIDER:
                provider_sat = min(1.0, inflight / self.provider_quota)

            out[lane] = LanePressure(
                lane=lane,
                queue_depth=depth,
                inflight=inflight,
                effective_backlog=eff,
                p95_latency_s=p95,
                latency_pressure=latency_pressure,
                underrun_pressure=underrun_pressure,
                provider_saturation=provider_sat,
            )
        return out
