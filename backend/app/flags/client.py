"""The SDK-style client facade — what application code actually calls.

``FlagsClient`` wraps a :class:`~app.flags.evaluator.FlagEvaluator` (over an
immutable snapshot) and an optional :class:`~app.flags.experiment.ExperimentEngine`
registry, and exposes the ergonomic, type-coerced accessors product code wants:

    client.bool_variation("live-video", ctx, default=False)
    client.string_variation("render-ladder", ctx, default="kenburns")
    client.int_variation("max-lookahead-shots", ctx, default=3)
    arm = client.assign("crew-vs-baseline", ctx)

Each variation call optionally fires an *exposure* through a pluggable
:class:`ExposureSink` (a sync or async callback), de-duplicated per unit, so the
experiment analysis only counts units that were actually shown a value. The sink
is the single seam where the in-process SDK connects to durable exposure
logging (the service wires a Postgres-backed sink); with no sink the client is
fully self-contained and infra-free.

The client never raises on evaluation — it always returns a usable value (the
caller's ``default`` for a missing flag, the flag's own variation otherwise).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.flags.context import EvalContext
from app.flags.evaluator import FlagEvaluator
from app.flags.experiment import (
    Assignment,
    Experiment,
    ExperimentEngine,
)
from app.flags.models import Evaluation, FlagSnapshot

#: An exposure record handed to the sink: (flag_or_experiment_key, context,
#: variation/variant key, dedup_key, payload).
ExposureCallback = Callable[["ExposureEvent"], None]
AsyncExposureCallback = Callable[["ExposureEvent"], Awaitable[None]]


class ExposureEvent:
    """A single recorded exposure (a unit was shown a flag value / experiment arm)."""

    __slots__ = ("context", "dedup_key", "kind", "payload", "subject_key", "variation")

    def __init__(
        self,
        *,
        kind: str,
        subject_key: str,
        variation: str | None,
        context: EvalContext,
        dedup_key: str | None,
        payload: dict[str, Any],
    ) -> None:
        self.kind = kind  # "flag" | "experiment"
        self.subject_key = subject_key
        self.variation = variation
        self.context = context
        self.dedup_key = dedup_key
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject_key": self.subject_key,
            "variation": self.variation,
            "dedup_key": self.dedup_key,
            "context": self.context.to_dict(),
            "payload": self.payload,
        }


@runtime_checkable
class ExposureSink(Protocol):
    """Where exposures go. Implementations may persist, queue, or count them."""

    def record(self, event: ExposureEvent) -> None:
        """Record one exposure (idempotent on ``event.dedup_key``)."""
        ...


class MemoryExposureSink:
    """An in-memory, de-duplicating sink — the default for tests / embeds."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.events: list[ExposureEvent] = []

    def record(self, event: ExposureEvent) -> None:
        if event.dedup_key is not None:
            if event.dedup_key in self._seen:
                return
            self._seen.add(event.dedup_key)
        self.events.append(event)

    def count(self, subject_key: str, variation: str) -> int:
        """How many distinct units were exposed to ``variation`` of ``subject_key``."""
        return sum(
            1 for e in self.events if e.subject_key == subject_key and e.variation == variation
        )


class FlagsClient:
    """The application-facing SDK over a snapshot + experiment registry."""

    def __init__(
        self,
        snapshot: FlagSnapshot,
        *,
        experiments: tuple[Experiment, ...] = (),
        default_salt: str = "",
        exposure_sink: ExposureSink | None = None,
        log_flag_exposures: bool = False,
    ) -> None:
        self._evaluator = FlagEvaluator(snapshot, default_salt=default_salt)
        self._experiments: dict[str, ExperimentEngine] = {
            e.key: ExperimentEngine(e) for e in experiments
        }
        self._sink = exposure_sink
        self._log_flag_exposures = log_flag_exposures

    # --- evaluation accessors ------------------------------------------ #

    def evaluate(self, flag_key: str, context: EvalContext, *, default: Any = None) -> Evaluation:
        """Full :class:`Evaluation` (value + reason + variation) for a flag."""
        result = self._evaluator.evaluate(flag_key, context, default=default)
        if self._log_flag_exposures and self._sink is not None and not result.is_default:
            self._sink.record(
                ExposureEvent(
                    kind="flag",
                    subject_key=flag_key,
                    variation=result.variation_key,
                    context=context,
                    dedup_key=None if context.anonymous else f"{flag_key}:{context.key}",
                    payload=result.to_dict(),
                )
            )
        return result

    def variation(self, flag_key: str, context: EvalContext, *, default: Any = None) -> Any:
        """The raw served value of ``flag_key`` (or ``default`` if absent)."""
        return self.evaluate(flag_key, context, default=default).value

    def bool_variation(self, flag_key: str, context: EvalContext, *, default: bool = False) -> bool:
        """Boolean accessor — coerces / falls back to ``default`` on a type mismatch."""
        value = self.variation(flag_key, context, default=default)
        return value if isinstance(value, bool) else default

    def string_variation(
        self, flag_key: str, context: EvalContext, *, default: str = ""
    ) -> str:
        """String accessor."""
        value = self.variation(flag_key, context, default=default)
        return value if isinstance(value, str) else default

    def int_variation(self, flag_key: str, context: EvalContext, *, default: int = 0) -> int:
        """Integer accessor (accepts int-valued numbers; rejects bool)."""
        value = self.variation(flag_key, context, default=default)
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return default

    def float_variation(
        self, flag_key: str, context: EvalContext, *, default: float = 0.0
    ) -> float:
        """Float accessor."""
        value = self.variation(flag_key, context, default=default)
        if isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        return default

    def json_variation(
        self, flag_key: str, context: EvalContext, *, default: Any = None
    ) -> Any:
        """JSON/object accessor (returns the value as-is)."""
        return self.variation(flag_key, context, default=default)

    def is_enabled(self, flag_key: str, context: EvalContext) -> bool:
        """Convenience for boolean gates: ``bool_variation(..., default=False)``."""
        return self.bool_variation(flag_key, context, default=False)

    # --- experiment accessors ------------------------------------------ #

    def assign(self, experiment_key: str, context: EvalContext) -> Assignment | None:
        """Assign ``context`` to ``experiment_key`` and log the exposure once.

        ``None`` if the experiment is unknown. The exposure is recorded only when
        the unit is actually enrolled (an arm was served) and de-duplicated by
        the engine's exposure key.
        """
        engine = self._experiments.get(experiment_key)
        if engine is None:
            return None
        assignment = engine.assign(context)
        if assignment.in_experiment and self._sink is not None:
            self._sink.record(
                ExposureEvent(
                    kind="experiment",
                    subject_key=experiment_key,
                    variation=assignment.variant_key,
                    context=context,
                    dedup_key=engine.exposure_key(context, assignment),
                    payload={
                        "variant": assignment.variant_key,
                        "version": assignment.experiment_version,
                    },
                )
            )
        return assignment

    def variant_key(self, experiment_key: str, context: EvalContext) -> str | None:
        """The assigned arm key (or ``None`` if not enrolled / unknown)."""
        assignment = self.assign(experiment_key, context)
        return assignment.variant_key if assignment is not None else None

    @property
    def snapshot_version(self) -> int:
        """The version of the snapshot this client serves (for cache coherence)."""
        return self._evaluator.snapshot.version

    @property
    def flag_keys(self) -> tuple[str, ...]:
        """All flag keys available to this client."""
        return self._evaluator.snapshot.keys()


__all__ = [
    "AsyncExposureCallback",
    "ExposureCallback",
    "ExposureEvent",
    "ExposureSink",
    "FlagsClient",
    "MemoryExposureSink",
]
