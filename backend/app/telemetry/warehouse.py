"""The §13 metrics warehouse — per-agent quality & cost aggregation.

kinora.md §13 asks for "a measurable efficiency gain over single-agent
baselines" and §12.5 for per-shot/per-session telemetry. The eval *harness*
(:mod:`app.eval`) computes the offline crew-vs-baseline report; this warehouse is
its **online** counterpart: a cheap, thread-safe, in-process accumulator that
watches the live crew and rolls up, *per agent role*, the numbers an operator and
the demo metrics panel care about:

* **cost** — model calls, input/output tokens, estimated USD, video-seconds;
* **latency** — call-count + total/percentile-able duration (Welford + a small
  reservoir so p50/p95 are available without storing every sample);
* **quality** — JSON-repair rate, tool-loop rounds, QA scores (CCS / style /
  motion), accepted vs degraded shots, regenerations.

Cardinality is bounded by construction: series are keyed by the **six fixed crew
roles** (plus an ``"other"`` bucket), never by session or shot. A snapshot is a
plain JSON-safe dict suitable for the warehouse read endpoint and for mirroring
into Prometheus gauges (see :meth:`MetricsWarehouse.export_prometheus`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# The six fixed crew roles (the §7 negotiation pipeline), plus a catch-all.
CREW_ROLES: tuple[str, ...] = (
    "showrunner",
    "adapter",
    "cinematographer",
    "generator",
    "critic",
    "continuity",
)
OTHER_ROLE = "other"

#: Bound on the per-agent latency reservoir (enough for stable p50/p95, cheap).
_RESERVOIR_CAP = 256


def normalize_role(name: str | None) -> str:
    """Map an arbitrary agent name onto a bounded role bucket."""
    if not name:
        return OTHER_ROLE
    lowered = name.strip().lower()
    return lowered if lowered in CREW_ROLES else OTHER_ROLE


@dataclass(slots=True)
class _Reservoir:
    """A tiny fixed-size sample buffer for percentile estimates (ring-evicting)."""

    cap: int = _RESERVOIR_CAP
    samples: list[float] = field(default_factory=list)
    _next: int = 0

    def add(self, value: float) -> None:
        if len(self.samples) < self.cap:
            self.samples.append(value)
        else:
            self.samples[self._next] = value
            self._next = (self._next + 1) % self.cap

    def percentile(self, q: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        if len(ordered) == 1:
            return ordered[0]
        rank = max(0.0, min(1.0, q)) * (len(ordered) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(ordered) - 1)
        frac = rank - lo
        return ordered[lo] * (1 - frac) + ordered[hi] * frac


@dataclass(slots=True)
class AgentStats:
    """Live rollup for one crew role."""

    role: str
    calls: int = 0
    errors: int = 0
    repairs: int = 0
    tool_rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_total_s: float = 0.0
    latency: _Reservoir = field(default_factory=_Reservoir)
    # Quality (recorded by the Critic / pipeline against the agent that produced
    # the shot — typically ``generator``).
    qa_ccs_sum: float = 0.0
    qa_ccs_n: int = 0
    qa_style_sum: float = 0.0
    qa_style_n: int = 0
    qa_motion_sum: float = 0.0
    qa_motion_n: int = 0
    shots_accepted: int = 0
    shots_degraded: int = 0
    regenerations: int = 0
    video_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def repair_rate(self) -> float:
        return self.repairs / self.calls if self.calls else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    @property
    def mean_latency_s(self) -> float:
        return self.latency_total_s / self.calls if self.calls else 0.0

    @property
    def mean_ccs(self) -> float | None:
        return self.qa_ccs_sum / self.qa_ccs_n if self.qa_ccs_n else None

    @property
    def mean_style_drift(self) -> float | None:
        return self.qa_style_sum / self.qa_style_n if self.qa_style_n else None

    @property
    def mean_motion(self) -> float | None:
        return self.qa_motion_sum / self.qa_motion_n if self.qa_motion_n else None

    @property
    def acceptance_rate(self) -> float | None:
        total = self.shots_accepted + self.shots_degraded
        return self.shots_accepted / total if total else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "repairs": self.repairs,
            "repair_rate": round(self.repair_rate, 4),
            "tool_rounds": self.tool_rounds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency": {
                "mean_s": round(self.mean_latency_s, 4),
                "p50_s": round(self.latency.percentile(0.5), 4),
                "p95_s": round(self.latency.percentile(0.95), 4),
                "total_s": round(self.latency_total_s, 4),
            },
            "quality": {
                "mean_ccs": _round_opt(self.mean_ccs),
                "mean_style_drift": _round_opt(self.mean_style_drift),
                "mean_motion": _round_opt(self.mean_motion),
                "shots_accepted": self.shots_accepted,
                "shots_degraded": self.shots_degraded,
                "acceptance_rate": _round_opt(self.acceptance_rate),
                "regenerations": self.regenerations,
            },
            "video_seconds": round(self.video_seconds, 3),
        }


def _round_opt(value: float | None, ndigits: int = 4) -> float | None:
    return round(value, ndigits) if value is not None else None


class MetricsWarehouse:
    """Thread-safe in-process rollup of per-agent quality + cost (§13).

    All record methods are cheap and lock-guarded so they are safe to call from
    the async API workers *and* the threaded render workers. The warehouse is a
    process-local singleton (see :func:`get_warehouse`); for cross-process totals
    an operator scrapes ``/metrics`` (the gauges this mirrors into) or runs the
    offline eval harness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, AgentStats] = {}

    def _stats(self, role: str) -> AgentStats:
        bucket = self._agents.get(role)
        if bucket is None:
            bucket = AgentStats(role=role)
            self._agents[role] = bucket
        return bucket

    # -- cost / latency recording ------------------------------------------- #

    def record_agent_call(
        self,
        agent: str,
        *,
        latency_s: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        repaired: bool = False,
        tool_rounds: int = 0,
        error: bool = False,
    ) -> None:
        """Record one completed agent model call (the §12.5 per-agent unit)."""
        role = normalize_role(agent)
        with self._lock:
            s = self._stats(role)
            s.calls += 1
            if error:
                s.errors += 1
            if repaired:
                s.repairs += 1
            s.tool_rounds += max(0, tool_rounds)
            s.input_tokens += max(0, input_tokens)
            s.output_tokens += max(0, output_tokens)
            s.cost_usd += max(0.0, cost_usd)
            latency = max(0.0, latency_s)
            s.latency_total_s += latency
            s.latency.add(latency)

    # -- quality recording -------------------------------------------------- #

    def record_qa(
        self,
        agent: str = "generator",
        *,
        ccs: float | None = None,
        style_drift: float | None = None,
        motion: float | None = None,
    ) -> None:
        """Record the Critic's QA scores against the producing agent (§9.5)."""
        role = normalize_role(agent)
        with self._lock:
            s = self._stats(role)
            if ccs is not None:
                s.qa_ccs_sum += ccs
                s.qa_ccs_n += 1
            if style_drift is not None:
                s.qa_style_sum += style_drift
                s.qa_style_n += 1
            if motion is not None:
                s.qa_motion_sum += motion
                s.qa_motion_n += 1

    def record_shot_outcome(
        self,
        agent: str = "generator",
        *,
        accepted: bool,
        regenerations: int = 0,
        video_seconds: float = 0.0,
    ) -> None:
        """Record a terminal shot outcome (accepted full footage vs degraded)."""
        role = normalize_role(agent)
        with self._lock:
            s = self._stats(role)
            if accepted:
                s.shots_accepted += 1
            else:
                s.shots_degraded += 1
            s.regenerations += max(0, regenerations)
            s.video_seconds += max(0.0, video_seconds)

    # -- snapshots ----------------------------------------------------------- #

    def agent(self, role: str) -> AgentStats | None:
        """Return a copy of one role's stats (``None`` if it has no activity)."""
        with self._lock:
            existing = self._agents.get(normalize_role(role))
            return _copy_stats(existing) if existing is not None else None

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe rollup: per-agent stats + crew totals + §13 derived."""
        with self._lock:
            agents = [_copy_stats(s) for s in self._agents.values()]
        agents.sort(key=lambda s: CREW_ROLES.index(s.role) if s.role in CREW_ROLES else 99)
        totals = _crew_totals(agents)
        return {
            "agents": [s.to_dict() for s in agents],
            "crew_totals": totals,
            "derived": _derived_metrics(agents, totals),
        }

    def export_prometheus(self) -> None:
        """Mirror the warehouse rollup into the observability Prometheus gauges.

        Imports lazily so the warehouse stays usable (and testable) without the
        Prometheus registry. A missing/erroring registry degrades to a no-op.
        """
        try:
            from app.telemetry import promstore
        except Exception:  # noqa: BLE001 - mirroring is best-effort
            return
        with self._lock:
            agents = [_copy_stats(s) for s in self._agents.values()]
        promstore.publish_warehouse(agents)

    def reset(self) -> None:
        """Drop all accumulated stats (between eval runs / tests)."""
        with self._lock:
            self._agents.clear()


def _copy_stats(s: AgentStats) -> AgentStats:
    clone = AgentStats(role=s.role)
    clone.calls = s.calls
    clone.errors = s.errors
    clone.repairs = s.repairs
    clone.tool_rounds = s.tool_rounds
    clone.input_tokens = s.input_tokens
    clone.output_tokens = s.output_tokens
    clone.cost_usd = s.cost_usd
    clone.latency_total_s = s.latency_total_s
    clone.latency = _Reservoir(cap=s.latency.cap, samples=list(s.latency.samples))
    clone.qa_ccs_sum = s.qa_ccs_sum
    clone.qa_ccs_n = s.qa_ccs_n
    clone.qa_style_sum = s.qa_style_sum
    clone.qa_style_n = s.qa_style_n
    clone.qa_motion_sum = s.qa_motion_sum
    clone.qa_motion_n = s.qa_motion_n
    clone.shots_accepted = s.shots_accepted
    clone.shots_degraded = s.shots_degraded
    clone.regenerations = s.regenerations
    clone.video_seconds = s.video_seconds
    return clone


def _crew_totals(agents: Iterable[AgentStats]) -> dict[str, Any]:
    calls = sum(s.calls for s in agents)
    errors = sum(s.errors for s in agents)
    repairs = sum(s.repairs for s in agents)
    in_tok = sum(s.input_tokens for s in agents)
    out_tok = sum(s.output_tokens for s in agents)
    cost = sum(s.cost_usd for s in agents)
    accepted = sum(s.shots_accepted for s in agents)
    degraded = sum(s.shots_degraded for s in agents)
    regens = sum(s.regenerations for s in agents)
    video = sum(s.video_seconds for s in agents)
    return {
        "calls": calls,
        "errors": errors,
        "error_rate": round(errors / calls, 4) if calls else 0.0,
        "repairs": repairs,
        "repair_rate": round(repairs / calls, 4) if calls else 0.0,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cost_usd": round(cost, 6),
        "shots_accepted": accepted,
        "shots_degraded": degraded,
        "regenerations": regens,
        "video_seconds": round(video, 3),
    }


def _derived_metrics(agents: list[AgentStats], totals: Mapping[str, Any]) -> dict[str, Any]:
    """The §13-flavoured headline numbers derived from the live rollup."""
    accepted = int(totals["shots_accepted"])
    degraded = int(totals["shots_degraded"])
    total_shots = accepted + degraded
    regens = int(totals["regenerations"])
    # Live acceptance rate ≈ accepted-footage efficiency proxy.
    acceptance_rate = (accepted / total_shots) if total_shots else None
    regen_rate = (regens / total_shots) if total_shots else None
    # Crew-wide QA means (weighted by sample count across agents).
    ccs_sum = sum(s.qa_ccs_sum for s in agents)
    ccs_n = sum(s.qa_ccs_n for s in agents)
    style_sum = sum(s.qa_style_sum for s in agents)
    style_n = sum(s.qa_style_n for s in agents)
    return {
        "total_shots": total_shots,
        "acceptance_rate": _round_opt(acceptance_rate),
        "regen_rate": _round_opt(regen_rate),
        "mean_ccs": _round_opt(ccs_sum / ccs_n if ccs_n else None),
        "mean_style_drift": _round_opt(style_sum / style_n if style_n else None),
        "cost_per_accepted_shot_usd": _round_opt(
            (float(totals["cost_usd"]) / accepted) if accepted else None, 6
        ),
        "tokens_per_call": _round_opt(
            (int(totals["total_tokens"]) / int(totals["calls"])) if totals["calls"] else None,
            1,
        ),
    }


# --------------------------------------------------------------------------- #
# Process-wide singleton.
# --------------------------------------------------------------------------- #

_warehouse_lock = threading.Lock()
_warehouse: MetricsWarehouse | None = None


def get_warehouse() -> MetricsWarehouse:
    """Return the process-wide metrics warehouse (created on first use)."""
    global _warehouse
    if _warehouse is None:
        with _warehouse_lock:
            if _warehouse is None:
                _warehouse = MetricsWarehouse()
    return _warehouse


def reset_warehouse() -> None:
    """Reset the process-wide warehouse (mainly for tests)."""
    get_warehouse().reset()


__all__ = [
    "CREW_ROLES",
    "OTHER_ROLE",
    "AgentStats",
    "MetricsWarehouse",
    "get_warehouse",
    "normalize_role",
    "reset_warehouse",
]
