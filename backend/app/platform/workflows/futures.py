"""``WorkflowFuture`` — the awaitable that suspends a workflow until history
resolves it.

Workflow code is an ``async def`` coroutine, but it does **not** run on a normal
event loop with real I/O. Instead the executor steps it by hand: each time the
workflow ``await``\\s something engine-provided (an activity, a timer, a child
workflow, a signal), it awaits a :class:`WorkflowFuture`. The future is either:

* **already resolved** — its result/exception is present in the replayed history,
  so the ``await`` returns/raises immediately and the workflow keeps running; or
* **unresolved** — there is no corresponding completion event yet, so awaiting it
  raises :class:`~app.platform.workflows.errors.WorkflowSuspended`, which the
  executor catches to *end the current task*. The workflow is parked; when the
  awaited event later arrives it is appended to history and the workflow is
  re-run from the top, this time finding the future resolved.

Because the workflow is re-run from the top on every task, futures are *recreated*
each task and matched to history by their command sequence number — they hold no
state across tasks themselves.

A handful of combinators (:func:`gather`, :func:`wait_any`) let workflow code
fan out concurrent activities deterministically: ordering is by command sequence,
never by wall-clock arrival, so concurrency is reproducible.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any, Generic, TypeVar

from app.platform.workflows.errors import WorkflowSuspended

T = TypeVar("T")

#: Sentinel distinguishing "no value yet" from a legitimately stored ``None``.
_UNSET = object()


class WorkflowFuture(Generic[T]):
    """A determinism-aware awaitable resolved from (replayed) history.

    The executor creates one per command, keyed by the command's ``seq``, and
    resolves it (:meth:`set_result` / :meth:`set_exception`) when it processes
    the matching completion event during replay. Workflow code obtains it from a
    context method (``ctx.execute_activity(...)`` etc.) and ``await``\\s it.
    """

    __slots__ = ("seq", "_result", "_exception", "_done")

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self._result: Any = _UNSET
        self._exception: BaseException | None = None
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def set_result(self, value: T) -> None:
        self._result = value
        self._done = True

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._done = True

    def result(self) -> T:
        if not self._done:
            raise WorkflowSuspended()
        if self._exception is not None:
            raise self._exception
        return self._result

    def __await__(self) -> Generator[Any, None, T]:
        if not self._done:
            # Park the workflow: no completion in history yet for this command.
            raise WorkflowSuspended()
        if self._exception is not None:
            raise self._exception
        return self._result
        # ``yield`` below is unreachable but makes this a generator so ``await``
        # accepts it; the early ``return`` above is what actually fires.
        yield  # pragma: no cover


def resolved(value: T) -> WorkflowFuture[T]:
    """A future that is already resolved with ``value`` (seq -1, not history-bound)."""
    fut: WorkflowFuture[T] = WorkflowFuture(-1)
    fut.set_result(value)
    return fut


async def gather(*futures: WorkflowFuture[Any]) -> list[Any]:
    """Await every future, returning results in argument order.

    Deterministic: it resolves them in the *given* order, suspending on the first
    unresolved one. Because the workflow re-runs from the top each task, once all
    the corresponding completions are in history this returns the full list in a
    single pass.
    """
    results: list[Any] = []
    for fut in futures:
        results.append(await fut)
    return results


async def wait_any(*futures: WorkflowFuture[Any]) -> tuple[int, Any]:
    """Return ``(index, result)`` of the first *already-resolved* future.

    Determinism rule: among futures resolved in the current history, the one with
    the **lowest command seq** wins (not wall-clock arrival), so the selection is
    reproducible. Suspends if none is resolved yet.
    """
    best: tuple[int, int, Any] | None = None  # (seq, index, result)
    for index, fut in enumerate(futures):
        if fut.done:
            try:
                value = fut.result()
            except BaseException as exc:  # noqa: BLE001 - propagate as the winner
                value = exc
            key = fut.seq if fut.seq >= 0 else index
            if best is None or key < best[0]:
                best = (key, index, value)
    if best is None:
        raise WorkflowSuspended()
    _, index, value = best
    if isinstance(value, BaseException):
        raise value
    return index, value


__all__ = ["WorkflowFuture", "gather", "resolved", "wait_any"]
