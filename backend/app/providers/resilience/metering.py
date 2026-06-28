"""Unified usage metering — fan-out + per-model rollups (§11.1 / §12.5).

Every provider call emits exactly one :class:`~app.providers.types.Usage`; the
round-1 client funnels it to a single ``usage_sink``. But several consumers want
that stream at once: the budget service (the hard video-seconds cap), the
:class:`~app.optim.cost_meter.CostMeter` (USD rollups), and the gateway's own
per-model telemetry. Wiring only one of them into ``create_providers`` would
starve the others.

:class:`MeteringSink` is the multiplexer that solves this *additively* — it **is**
a ``UsageSink`` (``Callable[[Usage], None]``), so it drops straight into the
existing ``create_providers(usage_sink=...)`` seam — and it:

* **fans out** every event to N downstream sinks (CostMeter, budget hook, …),
  isolating each so one broken sink never breaks a provider call or the others;
* keeps its own **per-model / per-operation rollups** of physical units (the same
  currency §11.1 cares about) for cheap gateway introspection; and
* exposes a **video-seconds total** the gateway can check against a budget floor
  to drive degradation decisions (without importing the budget service).

It never raises and never blocks meaningfully (a cheap lock guards the rollups),
so it is safe on every hot path.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.core.logging import get_logger

from ..types import Usage

logger = get_logger("app.providers.resilience.metering")

#: A downstream cost sink: receives every :class:`Usage`. Matches ``base.UsageSink``.
UsageSink = Callable[[Usage], None]


@dataclass
class MeterRollup:
    """Physical-unit accumulator for one slice of calls (model / op / total)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    errors: int = 0

    def add(self, usage: Usage) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.images += usage.images
        self.audio_seconds += usage.audio_seconds
        self.video_seconds += usage.video_seconds

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "images": self.images,
            "audio_seconds": round(self.audio_seconds, 3),
            "video_seconds": round(self.video_seconds, 3),
            "errors": self.errors,
        }


@dataclass
class MeterSnapshot:
    """A JSON-friendly view of the meter's rollups."""

    total: dict[str, float | int]
    by_model: dict[str, dict[str, float | int]]
    by_operation: dict[str, dict[str, float | int]]
    fanout_errors: int


class MeteringSink:
    """A fan-out ``UsageSink`` with per-model/op rollups and a video-seconds total.

    Construct with the downstream sinks to fan to (e.g. a ``CostMeter`` and a
    budget-recording callable). Calling ``meter(usage)`` records the rollups *then*
    forwards to each downstream, swallowing any downstream exception so a broken
    sink can never propagate into a provider call.
    """

    def __init__(self, downstreams: Iterable[UsageSink] | None = None) -> None:
        self._downstreams: list[UsageSink] = list(downstreams or [])
        self._lock = threading.Lock()
        self._total = MeterRollup()
        self._by_model: dict[str, MeterRollup] = {}
        self._by_operation: dict[str, MeterRollup] = {}
        self._fanout_errors = 0

    # -- composition ------------------------------------------------------ #

    def add_downstream(self, sink: UsageSink) -> None:
        """Attach another downstream sink (e.g. wire the budget late)."""
        with self._lock:
            self._downstreams.append(sink)

    @property
    def downstream_count(self) -> int:
        return len(self._downstreams)

    # -- the UsageSink protocol ------------------------------------------ #

    def __call__(self, usage: Usage) -> None:
        with self._lock:
            self._total.add(usage)
            self._by_model.setdefault(usage.model, MeterRollup()).add(usage)
            self._by_operation.setdefault(usage.operation, MeterRollup()).add(usage)
            downstreams = tuple(self._downstreams)
        for sink in downstreams:
            try:
                sink(usage)
            except Exception:  # noqa: BLE001 - a broken downstream must not break the call
                with self._lock:
                    self._fanout_errors += 1
                logger.warning("metering.downstream_error", model=usage.model, exc_info=True)

    def record_error(self, model: str, operation: str) -> None:
        """Note a *failed* call for a model/op (no Usage emitted on failure)."""
        with self._lock:
            self._total.errors += 1
            self._by_model.setdefault(model, MeterRollup()).errors += 1
            self._by_operation.setdefault(operation, MeterRollup()).errors += 1

    # -- introspection ---------------------------------------------------- #

    @property
    def video_seconds(self) -> float:
        """The total Wan video-seconds metered so far (the budget-critical unit)."""
        with self._lock:
            return self._total.video_seconds

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self._total.total_tokens

    def model_rollup(self, model: str) -> MeterRollup:
        """A *copy* of one model's rollup (never the live mutable object)."""
        with self._lock:
            roll = self._by_model.get(model)
            return _copy_rollup(roll) if roll is not None else MeterRollup()

    def snapshot(self) -> MeterSnapshot:
        with self._lock:
            return MeterSnapshot(
                total=self._total.as_dict(),
                by_model={k: v.as_dict() for k, v in self._by_model.items()},
                by_operation={k: v.as_dict() for k, v in self._by_operation.items()},
                fanout_errors=self._fanout_errors,
            )

    def reset(self) -> None:
        with self._lock:
            self._total = MeterRollup()
            self._by_model.clear()
            self._by_operation.clear()
            self._fanout_errors = 0


def _copy_rollup(roll: MeterRollup) -> MeterRollup:
    return MeterRollup(
        calls=roll.calls,
        input_tokens=roll.input_tokens,
        output_tokens=roll.output_tokens,
        images=roll.images,
        audio_seconds=roll.audio_seconds,
        video_seconds=roll.video_seconds,
        errors=roll.errors,
    )


__all__ = [
    "MeterRollup",
    "MeterSnapshot",
    "MeteringSink",
    "UsageSink",
]
