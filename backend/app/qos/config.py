"""Tunable QoS policy parameters (pydantic v2; additive, no infra).

Every knob the QoS fabric reads lives here as a frozen pydantic model so a caller
(the Scheduler, the worker pool, a test) can construct a policy with explicit,
validated parameters instead of magic numbers. Defaults mirror kinora.md §4.9/§12.2:
4 committed slots + 2 speculative + a small keyframe/cold pool, depth backpressure
at 64, per-session cap 6.

This is intentionally a plain ``BaseModel`` rather than ``BaseSettings`` — the
fabric is a pure policy layer with no environment of its own; the existing
``app.core.config.Settings`` stays the single source of process settings, and a
caller can build a :class:`QoSConfig` from it if it wants to wire env overrides.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.qos.model import QoSClass


class QoSConfig(BaseModel):
    """Frozen policy parameters for the whole QoS fabric."""

    model_config = {"frozen": True}

    # -- capacity (§4.9: 4 committed + 2 speculative + small cold pool) ------- #
    #: Total concurrent render slots the worker pool offers across all classes.
    total_slots: int = Field(default=6, gt=0)
    #: Reserved committed slots that speculative/cold can never occupy — protects
    #: committed latency even when lower classes flood the queue.
    committed_reserved_slots: int = Field(default=4, ge=0)

    # -- WFQ shares (strict-priority within a class; weighted across classes) -- #
    #: Relative service weights per class for the weighted-fair-queuing tier that
    #: runs *after* the committed reservation, so cold never fully starves.
    wfq_weights: dict[QoSClass, float] = Field(
        default_factory=lambda: {
            QoSClass.COMMITTED: 8.0,
            QoSClass.SPECULATIVE: 2.0,
            QoSClass.COLD: 1.0,
        }
    )

    # -- admission / backpressure (§12.2) ------------------------------------ #
    #: Total queued depth at/above which *new speculative* enqueues are shed.
    backpressure_depth: int = Field(default=64, gt=0)
    #: Total queued depth at/above which even speculative is *deferred* (soft
    #: signal to slow speculation upstream) before the hard shed threshold.
    speculation_slowdown_depth: int = Field(default=48, gt=0)
    #: Max concurrent renders one session may hold (per-session fairness, §12.2).
    session_cap: int = Field(default=6, gt=0)
    #: Max queued speculative items one book may hold before its excess is shed.
    per_book_speculative_cap: int = Field(default=16, gt=0)

    # -- aging / anti-starvation --------------------------------------------- #
    #: A queued item gains one priority boost-step per this many seconds waited.
    aging_step_s: float = Field(default=20.0, gt=0)
    #: Max class-steps a cold/speculative item may be promoted by aging (so a very
    #: old cold item can climb at most this far — never above committed by default).
    aging_max_boost: int = Field(default=1, ge=0)

    # -- deadline / EDF ------------------------------------------------------- #
    #: Slack at/below which an item is "urgent": its deadline drives ordering
    #: within its class ahead of FIFO. Beyond this, FIFO/age order applies.
    deadline_urgency_horizon_s: float = Field(default=30.0, gt=0)
    #: An item already this far past its deadline is a load-shed candidate: the
    #: reader has blown past it, so its video-seconds are wasted (negative slack).
    deadline_expiry_grace_s: float = Field(default=2.0, ge=0)

    # -- load shedding -------------------------------------------------------- #
    #: Target depth load-shedding drains *down to* when it fires (hysteresis floor).
    shed_target_depth: int = Field(default=56, gt=0)

    @model_validator(mode="after")
    def _check(self) -> QoSConfig:
        if self.committed_reserved_slots > self.total_slots:
            raise ValueError("committed_reserved_slots cannot exceed total_slots")
        for cls, w in self.wfq_weights.items():
            if w <= 0:
                raise ValueError(f"wfq weight for {cls.name} must be > 0")
        # The two soft thresholds are *derived* guards below the hard backpressure
        # depth; when a caller lowers ``backpressure_depth`` without restating them,
        # clamp the defaults down so the invariant
        # ``slowdown <= backpressure`` and ``shed_target <= backpressure`` always
        # holds. (frozen=True -> mutate via object.__setattr__.)
        if self.speculation_slowdown_depth > self.backpressure_depth:
            object.__setattr__(self, "speculation_slowdown_depth", self.backpressure_depth)
        if self.shed_target_depth > self.backpressure_depth:
            object.__setattr__(self, "shed_target_depth", self.backpressure_depth)
        return self

    def weight(self, qos_class: QoSClass) -> float:
        """The WFQ service weight for a class (default 1.0 if unspecified)."""
        return self.wfq_weights.get(qos_class, 1.0)


__all__ = ["QoSConfig"]
