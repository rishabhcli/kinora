"""Routing-decision telemetry + aggregate counters for the v2 router.

Every routing decision the router makes — which policy ran, the ranked order it
produced, whether stickiness fired, which backend won, how many attempts/hedges
it took, and the final outcome — is captured as a :class:`RouteDecision` and both
**structured-logged** (one event per render via structlog) and folded into a
running :class:`RouterMetrics` tally a ``/debug`` route or test can read.

No prompt content or URLs are ever logged — only backend names, counts, and
outcome labels — so this is safe to emit at INFO in production.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger("app.video.routing")

#: Injectable monotonic clock (seconds) for decision timing.
Clock = Callable[[], float]


@dataclass(slots=True)
class RouteDecision:
    """One render's routing decision + outcome, for logging and metrics.

    Built incrementally by the router as the render progresses, then emitted once
    on completion (success or terminal failure).
    """

    mode: str
    policy: str
    dispatch: str  # "failover" | "hedge"
    candidates: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)
    sticky_pinned: str | None = None
    sticky_hit: bool = False
    attempts: int = 0
    hedges_launched: int = 0
    winner: str | None = None
    outcome: str = "pending"  # "success" | "failover" | "gate" | "error" | "no_capable"
    error_class: str | None = None
    budget_low: bool = False

    def as_log_fields(self) -> dict[str, object]:
        """Structured-log-safe fields (names + counts only — never content)."""
        fields: dict[str, object] = {
            "mode": self.mode,
            "policy": self.policy,
            "dispatch": self.dispatch,
            "candidates": list(self.candidates),
            "ranked": list(self.ranked),
            "attempts": self.attempts,
            "outcome": self.outcome,
            "budget_low": self.budget_low,
        }
        if self.sticky_pinned is not None:
            fields["sticky_pinned"] = self.sticky_pinned
            fields["sticky_hit"] = self.sticky_hit
        if self.hedges_launched:
            fields["hedges_launched"] = self.hedges_launched
        if self.winner is not None:
            fields["winner"] = self.winner
        if self.error_class is not None:
            fields["error_class"] = self.error_class
        return fields


@dataclass
class RouterMetrics:
    """Aggregate counters across every routing decision (telemetry + tests)."""

    decisions: int = 0
    successes: int = 0
    failovers: int = 0  # renders that advanced past the first-ranked backend
    gate_propagations: int = 0  # LiveVideoDisabled propagations
    hard_errors: int = 0  # renders that exhausted every backend
    no_capable: int = 0  # renders with no backend able to serve the mode
    hedges_launched: int = 0
    sticky_hits: int = 0
    wins_by_backend: dict[str, int] = field(default_factory=dict)
    attempts_by_backend: dict[str, int] = field(default_factory=dict)
    errors_by_class: dict[str, int] = field(default_factory=dict)

    def record(self, decision: RouteDecision) -> None:
        """Fold one completed :class:`RouteDecision` into the running tally."""
        self.decisions += 1
        self.hedges_launched += decision.hedges_launched
        if decision.sticky_hit:
            self.sticky_hits += 1
        if decision.outcome == "success":
            self.successes += 1
            if decision.winner is not None:
                self.wins_by_backend[decision.winner] = (
                    self.wins_by_backend.get(decision.winner, 0) + 1
                )
            # A success that took more than one attempt means a failover happened.
            if decision.attempts > 1:
                self.failovers += 1
        elif decision.outcome == "gate":
            self.gate_propagations += 1
        elif decision.outcome == "no_capable":
            self.no_capable += 1
        else:  # "error" / anything terminal
            self.hard_errors += 1
        if decision.error_class is not None:
            self.errors_by_class[decision.error_class] = (
                self.errors_by_class.get(decision.error_class, 0) + 1
            )

    def record_attempt(self, backend: str) -> None:
        """Tally one backend attempt (every dispatch to a backend)."""
        self.attempts_by_backend[backend] = self.attempts_by_backend.get(backend, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "decisions": self.decisions,
            "successes": self.successes,
            "failovers": self.failovers,
            "gate_propagations": self.gate_propagations,
            "hard_errors": self.hard_errors,
            "no_capable": self.no_capable,
            "hedges_launched": self.hedges_launched,
            "sticky_hits": self.sticky_hits,
            "wins_by_backend": dict(self.wins_by_backend),
            "attempts_by_backend": dict(self.attempts_by_backend),
            "errors_by_class": dict(self.errors_by_class),
        }


def emit_decision(decision: RouteDecision) -> None:
    """Structured-log one completed routing decision at an outcome-aware level."""
    fields = decision.as_log_fields()
    if decision.outcome in ("success", "gate", "no_capable"):
        logger.info("video_routing.decision", **fields)
    else:
        logger.warning("video_routing.decision", **fields)


def now_ms(clock: Clock = time.monotonic) -> float:
    """Current monotonic time in milliseconds (for latency stamping)."""
    return clock() * 1000.0


__all__ = [
    "Clock",
    "RouteDecision",
    "RouterMetrics",
    "emit_decision",
    "now_ms",
]
