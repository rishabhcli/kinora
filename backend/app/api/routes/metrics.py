"""Eval API — the §13 metrics surface the Phase-10 frontend renders.

Two read endpoints, matching the shared frontend contract exactly:

* ``GET /api/eval/buffer-trace/{session_id}`` → the §4.10 committed-buffer
  sawtooth, **recomputed live** from the session's reading state by driving the
  real scheduler over the book's source-span index (zero video-seconds, §4.4).
  Returns ``[{t, committed_seconds_ahead, low, high}, …]``.

* ``GET /api/eval/report/{book_id}`` → the crew-vs-baseline §13 report
  (``ccs``/``efficiency``/``regen_rate``/``style_drift`` per arm, ``runs``,
  pre-registered ``thresholds``, ``per_character_ccs``). The eval run is
  expensive (it exercises the crew + baseline arms), so the endpoint serves the
  **cached** report produced by ``python -m app.eval.run`` rather than running a
  multi-minute job inside a request handler — exactly the "guard expensive runs"
  policy. The cheap buffer-trace, by contrast, is always recomputed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.db.repositories.session import SessionRepo
from app.db.repositories.shot import SourceSpanRepo
from app.eval.buffer_trace import DEFAULT_DURATION_S, simulate_buffer_trace

logger = get_logger("app.api.metrics")

router = APIRouter(prefix="/eval", tags=["eval"])

#: Bounds on the recomputed buffer trace so a request can never spin unbounded.
_MAX_DURATION_S = 1200.0
_MIN_DURATION_S = 10.0


def report_cache_key(book_id: str) -> str:
    """Redis key the §13 eval report is cached under (written by the CLI)."""
    return f"kinora:eval:report:{book_id}"


class BufferTracePoint(BaseModel):
    """One sample on the committed-buffer sawtooth (the shared contract item)."""

    t: float
    committed_seconds_ahead: float
    low: float
    high: float


async def _owned_book_id(container: Container, user: User, book_id: str) -> None:
    """404 unless ``book_id`` exists and is owned by ``user`` (ownership in Redis)."""
    owned = await container.redis.raw.sismember(f"kinora:user:{user.id}:books", book_id)
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or not owned:
        raise APIError("book_not_found", "no such book for this user", status=404)


@router.get("/buffer-trace/{session_id}", response_model=list[BufferTracePoint])
async def get_buffer_trace(
    session_id: str,
    container: ContainerDep,
    user: CurrentUser,
    velocity: Annotated[float | None, Query(gt=0, le=40)] = None,
    duration_s: Annotated[float | None, Query(ge=_MIN_DURATION_S, le=_MAX_DURATION_S)] = None,
) -> list[BufferTracePoint]:
    """Recompute the §4.10 watermark sawtooth for a session (zero video-seconds).

    Ownership is enforced against the durable session row; the live Scheduler
    control state (Redis) supplies the current focus word + velocity when present,
    otherwise the durable row's values are used.
    """
    async with container.session_factory() as session:
        row = await SessionRepo(session).get(session_id)
    if row is None or (row.user_id is not None and row.user_id != user.id):
        raise APIError("session_not_found", "no such session for this user", status=404)

    focus_word = row.focus_word
    velocity_wps = row.velocity_wps
    sched = await container.scheduler_store.load(session_id)
    if sched is not None:
        focus_word = sched.focus_word
        velocity_wps = sched.velocity_wps
    if velocity is not None:
        velocity_wps = velocity

    async with container.session_factory() as session:
        spans = SourceSpanRepo(session)
        result = await simulate_buffer_trace(
            shots=spans,
            book_id=row.book_id,
            focus_word=focus_word,
            velocity_wps=velocity_wps,
            settings=container.settings,
            duration_s=duration_s if duration_s is not None else DEFAULT_DURATION_S,
            session_id=session_id,
        )
    logger.info(
        "eval.buffer_trace_served",
        session_id=session_id,
        points=len(result.samples),
        video_seconds_spent=result.video_seconds_spent,
    )
    return [BufferTracePoint(**point) for point in result.to_contract()]


@router.get("/report/{book_id}")
async def get_report(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Return the cached crew-vs-baseline §13 report for a book (exact contract).

    The report is produced (and cached) by ``python -m app.eval.run --book
    <book_id>``; running both arms on demand inside the request path is
    deliberately avoided (it is expensive). A 404 with guidance is returned when
    no report has been cached yet.
    """
    await _owned_book_id(container, user, book_id)
    cached = await container.redis.get_json(report_cache_key(book_id))
    if not isinstance(cached, dict):
        raise APIError(
            "eval_report_not_ready",
            "no eval report cached for this book; run `python -m app.eval.run "
            f"--book {book_id}` to produce one",
            status=404,
        )
    return cached


__all__ = ["BufferTracePoint", "report_cache_key", "router"]
