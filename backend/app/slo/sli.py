"""Service-level *indicators* computed from the rolling metric streams (§12.5).

An SLI is a measured number: "the fraction of reads that were
buffer-underrun-free", "render p95 in ms", "shot-render success rate", "API
availability". Each :class:`SLIDefinition` binds a name to a *kind* (a ratio
"fraction good", or a latency percentile) and to the **stream key** it reads, so
the engine can compute it over any window.

This is deliberately separate from ``app.reliability.slo`` (SLIKind), which
operates on a finished load-test ``LoadReport``. Here the source is a *live*
rolling stream (see :mod:`app.slo.windows`), which is what a running service
needs for continuous error-budget burn tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.slo.windows import CounterStream, RatioWindow, SampleStream, SampleWindow


class SLIType(StrEnum):
    """How an indicator is computed from its stream."""

    #: Fraction of good events over total (a :class:`CounterStream`). The "good"
    #: direction is higher-is-better — e.g. underrun-free reads, shot success.
    RATIO_GOOD = "ratio_good"
    #: A latency percentile over a :class:`SampleStream` (lower-is-better).
    LATENCY_P50 = "latency_p50"
    LATENCY_P90 = "latency_p90"
    LATENCY_P95 = "latency_p95"
    LATENCY_P99 = "latency_p99"


_LATENCY_KIND = {
    SLIType.LATENCY_P50: "p50",
    SLIType.LATENCY_P90: "p90",
    SLIType.LATENCY_P95: "p95",
    SLIType.LATENCY_P99: "p99",
}


@dataclass(frozen=True, slots=True)
class SLIDefinition:
    """Binds an indicator name + type to the metric stream key it reads."""

    name: str
    type: SLIType
    stream: str
    #: Human description for the status report.
    description: str = ""
    #: Engineering unit for latency SLIs (display only).
    unit: str = ""

    @property
    def is_ratio(self) -> bool:
        return self.type is SLIType.RATIO_GOOD

    @property
    def higher_is_better(self) -> bool:
        """Ratio SLIs are higher-is-better; latency SLIs are lower-is-better."""
        return self.is_ratio


@dataclass(frozen=True, slots=True)
class SLIValue:
    """A computed indicator value over a window, plus the supporting tally."""

    definition: SLIDefinition
    value: float
    window_s: float
    #: Number of underlying observations (events / samples) in the window.
    sample_count: int
    #: True when the window had no data (value is the vacuous default).
    empty: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.definition.name,
            "type": self.definition.type.value,
            "value": round(self.value, 6),
            "window_s": self.window_s,
            "sample_count": self.sample_count,
            "empty": self.empty,
            "unit": self.definition.unit,
        }


def compute_ratio_sli(
    definition: SLIDefinition, win: RatioWindow, *, window_s: float
) -> SLIValue:
    """Build an :class:`SLIValue` from a counter window (fraction good)."""
    return SLIValue(
        definition=definition,
        value=win.ratio,
        window_s=window_s,
        sample_count=win.total,
        empty=win.total == 0,
    )


def compute_latency_sli(
    definition: SLIDefinition, win: SampleWindow, *, window_s: float
) -> SLIValue:
    """Build an :class:`SLIValue` from a sample window (a latency percentile)."""
    kind = _LATENCY_KIND[definition.type]
    return SLIValue(
        definition=definition,
        value=win.percentile(kind),
        window_s=window_s,
        sample_count=win.count,
        empty=win.is_empty,
    )


def compute_sli(
    definition: SLIDefinition,
    stream: CounterStream | SampleStream,
    *,
    now: float,
    window_s: float,
) -> SLIValue:
    """Compute an indicator from its stream over ``[now - window_s, now]``.

    Dispatches on the definition's type; raises ``TypeError`` if the stream
    flavour does not match the indicator type (a ratio SLI needs a
    :class:`CounterStream`; a latency SLI needs a :class:`SampleStream`).
    """
    if definition.is_ratio:
        if not isinstance(stream, CounterStream):
            raise TypeError(f"ratio SLI {definition.name!r} needs a CounterStream")
        return compute_ratio_sli(definition, stream.window(now=now, window_s=window_s),
                                 window_s=window_s)
    if not isinstance(stream, SampleStream):
        raise TypeError(f"latency SLI {definition.name!r} needs a SampleStream")
    return compute_latency_sli(definition, stream.window(now=now, window_s=window_s),
                               window_s=window_s)


# --------------------------------------------------------------------------- #
# The default Kinora product SLIs (the reliability target: "the next page's film
# is ready before the reader gets there").
# --------------------------------------------------------------------------- #

#: Canonical stream keys the engine wires its default SLIs to. Call sites emit
#: against these keys via :mod:`app.slo.service`.
STREAM_READ_UNDERRUN_FREE = "read.underrun_free"
STREAM_SHOT_SUCCESS = "shot.success"
STREAM_API_AVAILABILITY = "api.availability"
STREAM_RENDER_LATENCY_MS = "render.latency_ms"
STREAM_INTENT_LATENCY_MS = "api.intent_latency_ms"


DEFAULT_SLIS: tuple[SLIDefinition, ...] = (
    SLIDefinition(
        name="read_underrun_free",
        type=SLIType.RATIO_GOOD,
        stream=STREAM_READ_UNDERRUN_FREE,
        description="Fraction of page reads served without a buffer underrun (§4 core promise).",
    ),
    SLIDefinition(
        name="shot_success_rate",
        type=SLIType.RATIO_GOOD,
        stream=STREAM_SHOT_SUCCESS,
        description="Fraction of shot renders reaching an accepted asset, not DLQ/drop (§9.7).",
    ),
    SLIDefinition(
        name="api_availability",
        type=SLIType.RATIO_GOOD,
        stream=STREAM_API_AVAILABILITY,
        description="Fraction of API requests answered non-5xx (§12 availability).",
    ),
    SLIDefinition(
        name="render_latency_p95",
        type=SLIType.LATENCY_P95,
        stream=STREAM_RENDER_LATENCY_MS,
        description="p95 wall-clock to render a shot, ms (the buffer-fill budget).",
        unit="ms",
    ),
    SLIDefinition(
        name="intent_latency_p99",
        type=SLIType.LATENCY_P99,
        stream=STREAM_INTENT_LATENCY_MS,
        description="p99 of the §4.9 control-tick (intent) latency, ms.",
        unit="ms",
    ),
)


__all__ = [
    "DEFAULT_SLIS",
    "SLIDefinition",
    "SLIType",
    "SLIValue",
    "STREAM_API_AVAILABILITY",
    "STREAM_INTENT_LATENCY_MS",
    "STREAM_READ_UNDERRUN_FREE",
    "STREAM_RENDER_LATENCY_MS",
    "STREAM_SHOT_SUCCESS",
    "compute_latency_sli",
    "compute_ratio_sli",
    "compute_sli",
]
