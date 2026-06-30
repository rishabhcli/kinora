"""The SLO engine: registers metric streams, computes SLIs, tracks error budgets,
fires multi-window burn alerts, and exposes the release gate + status report.

The engine is the one stateful object the app holds. Call sites *record* good/bad
events and latency samples against named streams (``record_read``,
``record_shot``, ``record_request``, ``observe_render_latency`` …); the engine
keeps them in bounded rolling windows (:mod:`app.slo.windows`). On demand it
computes a :class:`SLOStatus` snapshot — every SLI over a short eval window, the
error-budget state over each objective's long window, the burn-rate alert per
objective, and the derived release gate.

Clock-injected: every read/record takes ``now`` (defaulting to ``time.time()``)
so tests drive a synthetic clock and assert exact burn rates. No I/O, no infra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from app.slo.gate import GateConfig, GateDecision, GateResult, decide_gate
from app.slo.objectives import (
    AlertSeverity,
    BudgetState,
    BurnAlert,
    LatencyObjective,
    LatencyVerdict,
    MultiWindowBurnPolicy,
    Objective,
    default_burn_policy,
)
from app.slo.sli import (
    DEFAULT_SLIS,
    SLIDefinition,
    SLIValue,
    compute_sli,
)
from app.slo.windows import CounterStream, SampleStream

logger = structlog.get_logger(__name__)

#: Window the *current* SLI snapshot is computed over (the dashboard "now" read).
DEFAULT_EVAL_WINDOW_S = 300.0


@dataclass(frozen=True, slots=True)
class SLOStatus:
    """A point-in-time snapshot of the whole SLO plane."""

    at: float
    slis: tuple[SLIValue, ...]
    budgets: tuple[BudgetState, ...]
    alerts: tuple[BurnAlert, ...]
    latency: tuple[LatencyVerdict, ...]
    gate: GateResult

    @property
    def healthy(self) -> bool:
        """True when no objective is exhausted, no alert is firing, no latency miss."""
        return (
            not any(b.is_exhausted for b in self.budgets)
            and not any(a.firing for a in self.alerts)
            and all(v.met for v in self.latency)
        )

    @property
    def firing_alerts(self) -> tuple[BurnAlert, ...]:
        return tuple(a for a in self.alerts if a.firing)

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "healthy": self.healthy,
            "gate": self.gate.to_dict(),
            "slis": [s.to_dict() for s in self.slis],
            "error_budgets": [b.to_dict() for b in self.budgets],
            "burn_alerts": [a.to_dict() for a in self.alerts],
            "latency_objectives": [v.to_dict() for v in self.latency],
        }


@dataclass(slots=True)
class SLOEngine:
    """Holds the live metric streams and the configured objectives.

    Streams are created lazily on first record/registration. ``horizon_s`` is the
    longest window any objective / burn policy needs, so every stream retains at
    least that much history (defaults to the policy's longest window).
    """

    objectives: list[Objective] = field(default_factory=list)
    latency_objectives: list[LatencyObjective] = field(default_factory=list)
    slis: list[SLIDefinition] = field(default_factory=list)
    burn_policy: MultiWindowBurnPolicy = field(default_factory=default_burn_policy)
    gate_config: GateConfig = field(default_factory=GateConfig)
    eval_window_s: float = DEFAULT_EVAL_WINDOW_S
    _counters: dict[str, CounterStream] = field(default_factory=dict)
    _samples: dict[str, SampleStream] = field(default_factory=dict)

    # -- registration -------------------------------------------------------- #

    def _horizon(self) -> float:
        """The longest window across budget windows + burn-policy windows."""
        windows = [self.eval_window_s]
        windows.extend(o.window_s for o in self.objectives)
        windows.extend(o.window_s for o in self.latency_objectives)
        windows.extend(self.burn_policy.window_lengths)
        return max(windows, default=self.eval_window_s)

    def register_sli(self, definition: SLIDefinition) -> None:
        """Register an SLI (and create its backing stream if absent)."""
        self.slis = [s for s in self.slis if s.name != definition.name]
        self.slis.append(definition)
        self._ensure_stream(definition)

    def register_objective(self, objective: Objective) -> None:
        self.objectives = [o for o in self.objectives if o.name != objective.name]
        self.objectives.append(objective)
        self.register_sli(objective.sli)

    def register_latency_objective(self, objective: LatencyObjective) -> None:
        self.latency_objectives = [
            o for o in self.latency_objectives if o.name != objective.name
        ]
        self.latency_objectives.append(objective)
        self.register_sli(objective.sli)

    def _ensure_stream(self, definition: SLIDefinition) -> None:
        horizon = self._horizon()
        if definition.is_ratio:
            stream = self._counters.get(definition.stream)
            if stream is None:
                self._counters[definition.stream] = CounterStream(horizon_s=horizon)
            else:
                stream.horizon_s = max(stream.horizon_s, horizon)
        else:
            sstream = self._samples.get(definition.stream)
            if sstream is None:
                self._samples[definition.stream] = SampleStream(horizon_s=horizon)
            else:
                sstream.horizon_s = max(sstream.horizon_s, horizon)

    # -- recording (call-site facing) ---------------------------------------- #

    def record_event(
        self, stream: str, *, good: bool, now: float | None = None, weight: int = 1
    ) -> None:
        """Record a good/bad event against a ratio stream (creates it if needed)."""
        s = self._counters.get(stream)
        if s is None:
            s = self._counters[stream] = CounterStream(horizon_s=self._horizon())
        s.record(good=good, now=_now(now), weight=weight)

    def record_sample(self, stream: str, value: float, *, now: float | None = None) -> None:
        """Record a numeric observation against a latency stream (creates if needed)."""
        s = self._samples.get(stream)
        if s is None:
            s = self._samples[stream] = SampleStream(horizon_s=self._horizon())
        s.record(value, now=_now(now))

    # -- computation --------------------------------------------------------- #

    def compute_sli(
        self, definition: SLIDefinition, *, now: float | None = None, window_s: float | None = None
    ) -> SLIValue:
        """Compute one SLI over ``window_s`` (default: the eval window)."""
        win = self.eval_window_s if window_s is None else window_s
        stream: CounterStream | SampleStream | None
        if definition.is_ratio:
            stream = self._counters.get(definition.stream)
            if stream is None:
                stream = CounterStream(horizon_s=self._horizon())
        else:
            stream = self._samples.get(definition.stream)
            if stream is None:
                stream = SampleStream(horizon_s=self._horizon())
        return compute_sli(definition, stream, now=_now(now), window_s=win)

    def budget_state(self, objective: Objective, *, now: float | None = None) -> BudgetState:
        """Error-budget accounting for ``objective`` over its long budget window."""
        val = self.compute_sli(objective.sli, now=now, window_s=objective.window_s)
        return BudgetState(
            objective=objective,
            good_ratio=val.value,
            sample_count=val.sample_count,
        )

    def burn_alert(self, objective: Objective, *, now: float | None = None) -> BurnAlert:
        """Evaluate the multi-window burn policy for ``objective``."""
        at = _now(now)
        failure_by_window: dict[float, float] = {}
        for w in self.burn_policy.window_lengths:
            val = self.compute_sli(objective.sli, now=at, window_s=w)
            # failure ratio = 1 - good ratio; an empty window (value==1.0) => 0 burn.
            failure_by_window[w] = 1.0 - val.value
        return self.burn_policy.evaluate(objective, failure_by_window=failure_by_window)

    def latency_verdict(
        self, objective: LatencyObjective, *, now: float | None = None
    ) -> LatencyVerdict:
        val = self.compute_sli(objective.sli, now=now, window_s=objective.window_s)
        return objective.evaluate(val)

    def status(self, *, now: float | None = None) -> SLOStatus:
        """Compute the full SLO snapshot + the derived release gate."""
        at = _now(now)
        slis = tuple(self.compute_sli(d, now=at) for d in self.slis)
        budgets = tuple(self.budget_state(o, now=at) for o in self.objectives)
        alerts = tuple(self.burn_alert(o, now=at) for o in self.objectives)
        latency = tuple(self.latency_verdict(o, now=at) for o in self.latency_objectives)
        # Build a draft (gate is computed *from* the snapshot), then the final one.
        draft = SLOStatus(
            at=at, slis=slis, budgets=budgets, alerts=alerts, latency=latency, gate=_DRAFT_GATE
        )
        gate = decide_gate(draft, config=self.gate_config)
        if any(a.severity is AlertSeverity.PAGE for a in alerts):
            logger.warning(
                "slo.page", alerts=[a.objective_name for a in draft.firing_alerts]
            )
        return SLOStatus(
            at=at, slis=slis, budgets=budgets, alerts=alerts, latency=latency, gate=gate
        )

    def release_gate(self, *, now: float | None = None) -> GateResult:
        """The release-gate decision the flag / canary systems consult."""
        return self.status(now=now).gate


def _now(now: float | None) -> float:
    return time.time() if now is None else now


# A throwaway gate used while assembling the draft SLOStatus the real gate is
# computed from (decide_gate needs a status object). Never surfaced to callers.
_DRAFT_GATE = GateResult(decision=GateDecision.ALLOW, reasons=(), min_budget_remaining=1.0)


# --------------------------------------------------------------------------- #
# Default Kinora engine
# --------------------------------------------------------------------------- #


def build_default_engine(
    *,
    read_target: float = 0.99,
    shot_target: float = 0.98,
    availability_target: float = 0.995,
    render_p95_ms: float = 8000.0,
    intent_p99_ms: float = 250.0,
    eval_window_s: float = DEFAULT_EVAL_WINDOW_S,
) -> SLOEngine:
    """The standard Kinora product SLO engine (the §12.5 / §4 reliability target).

    * ``read_underrun_free`` — the core promise: the next page's film is ready.
    * ``shot_success_rate`` — the render pipeline reaches an accepted asset.
    * ``api_availability`` — the API answers non-5xx.
    * ``render_latency_p95`` / ``intent_latency_p99`` — the buffer-fill + control
      tick latency budgets.
    """
    by_name = {d.name: d for d in DEFAULT_SLIS}
    engine = SLOEngine(eval_window_s=eval_window_s)
    engine.register_objective(
        Objective("read-underrun-free", by_name["read_underrun_free"], read_target)
    )
    engine.register_objective(
        Objective("shot-success", by_name["shot_success_rate"], shot_target)
    )
    engine.register_objective(
        Objective("api-availability", by_name["api_availability"], availability_target)
    )
    engine.register_latency_objective(
        LatencyObjective("render-p95", by_name["render_latency_p95"], render_p95_ms)
    )
    engine.register_latency_objective(
        LatencyObjective("intent-p99", by_name["intent_latency_p99"], intent_p99_ms)
    )
    return engine


def engine_from_settings(settings: object) -> SLOEngine:
    """Build the default engine from application :class:`Settings` (additive cfg)."""
    return build_default_engine(
        read_target=float(getattr(settings, "slo_read_underrun_free_target", 0.99)),
        shot_target=float(getattr(settings, "slo_shot_success_target", 0.98)),
        availability_target=float(getattr(settings, "slo_availability_target", 0.995)),
        render_p95_ms=float(getattr(settings, "slo_render_p95_ms", 8000.0)),
        intent_p99_ms=float(getattr(settings, "slo_intent_p99_ms", 250.0)),
        eval_window_s=float(getattr(settings, "slo_eval_window_s", DEFAULT_EVAL_WINDOW_S)),
    )


__all__ = [
    "DEFAULT_EVAL_WINDOW_S",
    "SLOEngine",
    "SLOStatus",
    "build_default_engine",
    "engine_from_settings",
]
