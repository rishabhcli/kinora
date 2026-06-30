"""The observability facade — one clean ``@traced`` / ``span()`` API (§12).

This is the surface call sites use to instrument the render / provider stack. It
ties three planes together behind a single, ergonomic call:

* **tracing** — opens a span on the dependency-free tracer
  (:mod:`app.telemetry.spans`), which transparently bridges to a real
  OpenTelemetry span when the SDK + an OTLP endpoint are present. The trace/span
  context vars propagate across ``await``, so a single shot render produces *one*
  trace spanning the scheduler decision → the §9.7 pipeline state machine → each
  provider call → persist.
* **log enrichment** — binds the §12 domain ids (book/session/shot/provider/
  render-state) onto the contextvars spine (:mod:`app.observability.enrichment`),
  so every log line and span emitted underneath carries them automatically.
* **metrics** — on span close, records the matching Prometheus series
  (:mod:`app.observability.metrics`): provider latency/error-rate for a provider
  span, render latency for a render span.

Everything degrades to a cheap no-op: with no OTLP endpoint the tracer just keeps
an in-process span tree (or drops spans entirely under the default null
exporter), and the metric helpers write to a private registry that nothing has to
scrape. Nothing here calls a model, opens a socket, or requires OpenTelemetry.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any, ParamSpec, TypeVar, cast

from app.observability import metrics
from app.observability.enrichment import bind_render_context, reset_render_context
from app.telemetry.spans import Span
from app.telemetry.spans import span as _telemetry_span

#: Span-attribute namespace prefix for Kinora-domain attributes.
ATTR_BOOK = "kinora.book_id"
ATTR_SESSION = "kinora.session_id"
ATTR_SHOT = "kinora.shot_id"
ATTR_PROVIDER = "kinora.provider"
ATTR_OP = "kinora.op"
ATTR_RENDER_MODE = "kinora.render_mode"
ATTR_RENDER_STATE = "kinora.render_state"

P = ParamSpec("P")
R = TypeVar("R")


def _domain_attributes(
    *,
    book_id: str | None,
    session_id: str | None,
    shot_id: str | None,
    provider: str | None,
    render_state: str | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the span attribute dict for a domain-scoped span (skips ``None``)."""
    attrs: dict[str, Any] = {}
    if book_id is not None:
        attrs[ATTR_BOOK] = book_id
    if session_id is not None:
        attrs[ATTR_SESSION] = session_id
    if shot_id is not None:
        attrs[ATTR_SHOT] = shot_id
    if provider is not None:
        attrs[ATTR_PROVIDER] = provider
    if render_state is not None:
        attrs[ATTR_RENDER_STATE] = render_state
    if extra:
        attrs.update(extra)
    return attrs


@contextlib.contextmanager
def span(
    name: str,
    *,
    book_id: str | None = None,
    session_id: str | None = None,
    shot_id: str | None = None,
    provider: str | None = None,
    render_state: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Open a domain-scoped span: trace + log-context + auto error capture.

    The given domain ids are *both* set as span attributes and bound onto the
    log-enrichment contextvars for the duration of the block, so descendant spans
    and every log line inside carry them. An exception inside marks the span
    errored (the telemetry tracer records the exception) and is re-raised; the log
    context is always restored on exit.
    """
    tokens = bind_render_context(
        book_id=book_id,
        session_id=session_id,
        shot_id=shot_id,
        provider=provider,
        render_state=render_state,
    )
    attrs = _domain_attributes(
        book_id=book_id,
        session_id=session_id,
        shot_id=shot_id,
        provider=provider,
        render_state=render_state,
        extra=attributes,
    )
    try:
        with _telemetry_span(name, attributes=attrs) as s:
            yield s
    finally:
        reset_render_context(tokens)


@contextlib.contextmanager
def render_span(
    name: str,
    *,
    shot_id: str,
    mode: str | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
    render_state: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    record_latency: bool = True,
) -> Iterator[Span]:
    """Span one render step and record its latency to the §12.5 histogram.

    On a clean close the elapsed wall-clock is recorded against
    ``kinora_render_latency_seconds{mode=…}`` (labelled by ``mode`` when given,
    else the span ``name``). An error inside skips the latency observation (a
    failed render's wall-clock would skew the latency SLI) but still propagates.
    """
    extra: dict[str, Any] = dict(attributes or {})
    if mode is not None:
        extra[ATTR_RENDER_MODE] = mode
    started = time.perf_counter()
    errored = False
    with span(
        name,
        book_id=book_id,
        session_id=session_id,
        shot_id=shot_id,
        render_state=render_state,
        attributes=extra,
    ) as s:
        try:
            yield s
        except BaseException:
            errored = True
            raise
        finally:
            if record_latency and not errored:
                metrics.observe_render_latency(mode or name, time.perf_counter() - started)


@contextlib.contextmanager
def provider_span(
    op: str,
    *,
    model: str,
    book_id: str | None = None,
    session_id: str | None = None,
    shot_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span]:
    """Span one provider call and record its call/latency/error metrics.

    On exit :func:`app.observability.metrics.observe_provider` is called exactly
    once: a success records latency, a failure increments the per-(model, op)
    error counter. ``op`` is the logical operation (``t2v`` / ``i2v`` / ``tts`` /
    ``image`` / ``chat``); ``model`` is the concrete provider model id.
    """
    extra: dict[str, Any] = dict(attributes or {})
    extra[ATTR_OP] = op
    started = time.perf_counter()
    ok = True
    with span(
        f"provider.{op}",
        book_id=book_id,
        session_id=session_id,
        shot_id=shot_id,
        provider=model,
        attributes=extra,
    ) as s:
        try:
            yield s
        except BaseException:
            ok = False
            raise
        finally:
            metrics.observe_provider(
                model=model, op=op, latency_s=time.perf_counter() - started, ok=ok
            )


def traced(
    name: str | None = None,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: wrap a sync or async function in a span.

    The span name defaults to ``module.qualname``. Works transparently on both
    coroutine and plain functions — the returned wrapper preserves the original's
    awaitability. Static attributes given here are attached on every call. The
    domain ids are *not* read from arguments (keep that explicit at the call site);
    this decorator is for cheap "trace this function" instrumentation.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        span_name = name or f"{func.__module__}.{func.__qualname__}"

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                with span(span_name, attributes=attributes):
                    afunc = cast(Callable[P, Awaitable[R]], func)
                    return await afunc(*args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(span_name, attributes=attributes):
                return func(*args, **kwargs)

        return cast(Callable[P, R], sync_wrapper)

    return decorator


__all__ = [
    "ATTR_BOOK",
    "ATTR_OP",
    "ATTR_PROVIDER",
    "ATTR_RENDER_MODE",
    "ATTR_RENDER_STATE",
    "ATTR_SESSION",
    "ATTR_SHOT",
    "provider_span",
    "render_span",
    "span",
    "traced",
]
