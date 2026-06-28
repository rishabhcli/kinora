"""Crew tracing — spans + warehouse rollups threaded across the six agents.

The §7 negotiation runs as a pipeline:

    showrunner → adapter → cinematographer → generator → critic → continuity

Each step is a model call (or several) made by one agent. This module gives that
pipeline two things at once, in a single ergonomic wrapper:

* a **span** under the active trace, so the whole negotiation shows up as one
  trace with a child span per agent (and logs inside each step carry the right
  ``trace_id`` / ``span_id`` via the context vars);
* a **warehouse rollup**, so per-agent latency / tokens / cost / repair-rate /
  QA accumulate live (the online §13 surface).

Two entry points:

* :func:`agent_span` — a context manager you wrap an agent call in. It opens the
  span, records timing into the warehouse on exit, and stamps tokens/cost/repair
  via :meth:`CrewCall.record`.
* :func:`traced_agent_call` — an async helper that wraps a coroutine, deriving the
  token delta from a usage-totals accessor automatically.

Neither calls a model; both are pure instrumentation and safe with KINORA_LIVE
off and zero credits.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.telemetry.spans import STATUS_ERROR, Span, span
from app.telemetry.warehouse import MetricsWarehouse, get_warehouse

T = TypeVar("T")

#: Span name prefix for crew agent spans (``crew.generator`` etc.).
SPAN_PREFIX = "crew"


@dataclass(slots=True)
class CrewCall:
    """Handle for an in-flight agent span; record cost/quality before it ends.

    Mutated inside an :func:`agent_span` block; on block exit the accumulated
    values are written to the warehouse and the span attributes.
    """

    agent: str
    span: Span
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    repaired: bool = False
    tool_rounds: int = 0
    error: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        repaired: bool | None = None,
        tool_rounds: int | None = None,
    ) -> CrewCall:
        """Set token / cost / repair attributes for the call (chainable)."""
        if input_tokens is not None:
            self.input_tokens = input_tokens
        if output_tokens is not None:
            self.output_tokens = output_tokens
        if cost_usd is not None:
            self.cost_usd = cost_usd
        if repaired is not None:
            self.repaired = repaired
        if tool_rounds is not None:
            self.tool_rounds = tool_rounds
        return self

    def add_tokens(self, *, input_tokens: int = 0, output_tokens: int = 0) -> CrewCall:
        """Accumulate token usage (for multi-round tool loops)."""
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        return self

    def set_attribute(self, key: str, value: Any) -> CrewCall:
        """Attach an attribute to the span and the warehouse extra."""
        self.extra[key] = value
        self.span.set_attribute(key, value)
        return self


@contextlib.contextmanager
def agent_span(
    agent: str,
    *,
    operation: str | None = None,
    warehouse: MetricsWarehouse | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[CrewCall]:
    """Trace one agent step + roll it into the warehouse.

    Args:
        agent: the crew role (``showrunner`` … ``continuity``).
        operation: optional sub-operation (e.g. ``plan_production``) added as a
            span attribute and appended to the span name.
        warehouse: override the process warehouse (tests).
        attributes: extra span attributes recorded at open.

    Yields a :class:`CrewCall`; call :meth:`CrewCall.record` (or ``add_tokens``)
    inside the block. Timing is measured automatically; an exception marks the
    span errored, records the error in the warehouse, and re-raises.
    """
    wh = warehouse or get_warehouse()
    span_name = f"{SPAN_PREFIX}.{agent}" + (f".{operation}" if operation else "")
    attrs: dict[str, Any] = {"agent": agent}
    if operation:
        attrs["operation"] = operation
    if attributes:
        attrs.update(attributes)

    started = time.monotonic()
    with span(span_name, attributes=attrs) as sp:
        call = CrewCall(agent=agent, span=sp)
        try:
            yield call
        except BaseException:
            call.error = True
            sp.set_status(STATUS_ERROR)
            raise
        finally:
            latency = max(0.0, time.monotonic() - started)
            sp.set_attribute("latency_ms", round(latency * 1000, 3))
            sp.set_attribute("tokens.input", call.input_tokens)
            sp.set_attribute("tokens.output", call.output_tokens)
            if call.repaired:
                sp.set_attribute("json.repaired", True)
            wh.record_agent_call(
                agent,
                latency_s=latency,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_usd=call.cost_usd,
                repaired=call.repaired,
                tool_rounds=call.tool_rounds,
                error=call.error,
            )


async def traced_agent_call(
    agent: str,
    coro_factory: Callable[[], Awaitable[T]],
    *,
    operation: str | None = None,
    tokens_before: int | None = None,
    tokens_after: Callable[[], int | None] | None = None,
    warehouse: MetricsWarehouse | None = None,
) -> T:
    """Run an agent coroutine inside :func:`agent_span`, deriving the token delta.

    ``tokens_after`` (typically ``lambda: providers.client.usage_totals.total_tokens``)
    is read after the call; the delta against ``tokens_before`` is attributed to
    the agent as output tokens (a coarse but useful per-agent token signal when a
    finer input/output split is unavailable).
    """
    with agent_span(agent, operation=operation, warehouse=warehouse) as call:
        result = await coro_factory()
        after = tokens_after() if tokens_after is not None else None
        if after is not None and tokens_before is not None:
            call.add_tokens(output_tokens=max(0, after - tokens_before))
        return result


def record_qa(
    *,
    agent: str = "generator",
    ccs: float | None = None,
    style_drift: float | None = None,
    motion: float | None = None,
    warehouse: MetricsWarehouse | None = None,
) -> None:
    """Record QA scores against the producing agent."""
    wh = warehouse or get_warehouse()
    wh.record_qa(agent, ccs=ccs, style_drift=style_drift, motion=motion)


def record_shot_outcome(
    *,
    agent: str = "generator",
    accepted: bool,
    regenerations: int = 0,
    video_seconds: float = 0.0,
    warehouse: MetricsWarehouse | None = None,
) -> None:
    """Record a terminal shot outcome into the warehouse."""
    wh = warehouse or get_warehouse()
    wh.record_shot_outcome(
        agent, accepted=accepted, regenerations=regenerations, video_seconds=video_seconds
    )


__all__ = [
    "SPAN_PREFIX",
    "CrewCall",
    "agent_span",
    "record_qa",
    "record_shot_outcome",
    "traced_agent_call",
]
