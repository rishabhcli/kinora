"""The durable run store — the persistence seam that makes crashes survivable.

The engine never holds run state only in memory: after every step (and every
timer/signal transition) it writes the :class:`~app.sagas.history.RunState`
through a :class:`DurableStore`. A crash therefore loses at most the *in-flight*
step, and a fresh engine reloads the last persisted state and resumes from the
cursor.

:class:`DurableStore` is a Protocol so production can back it with Postgres or
Redis while tests use :class:`InMemoryDurableStore` here; both honour the same
two invariants:

* **optimistic concurrency** — :meth:`DurableStore.save` takes the
  ``expected_revision`` the caller read; if the stored revision has moved, the
  store raises :class:`~app.sagas.errors.StoreConflictError`. This is how two
  engines (a worker and the recovery sweep) can't both drive one run.
* **JSON round-trip fidelity** — what you save is what you load, byte-stable, so
  a snapshot survives a process boundary.

The in-memory store also implements :meth:`due_runs` (timers ready to fire) and
:meth:`stuck_runs` (in-flight runs whose lease expired) so the recovery sweep is
testable without a real database.
"""

from __future__ import annotations

import copy
import threading
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger
from app.sagas.errors import RunNotFoundError, StoreConflictError
from app.sagas.history import TERMINAL_RUN_STATUSES, RunState, RunStatus

logger = get_logger("app.sagas.store")


@runtime_checkable
class DurableStore(Protocol):
    """Persistence for workflow runs (production: DB/Redis; tests: in-memory)."""

    async def create(self, state: RunState) -> RunState:
        """Persist a brand-new run (revision must be 0). Returns the stored copy."""
        ...

    async def load(self, run_id: str) -> RunState:
        """Load a run; raise :class:`RunNotFoundError` if absent."""
        ...

    async def try_load(self, run_id: str) -> RunState | None:
        """Load a run or ``None`` if absent."""
        ...

    async def save(self, state: RunState, *, expected_revision: int) -> RunState:
        """Compare-and-set persist.

        Bumps ``revision`` to ``expected_revision + 1`` on success; raises
        :class:`StoreConflictError` if the stored revision != ``expected_revision``.
        Returns the stored copy (with the new revision).
        """
        ...

    async def due_runs(self, now: float, *, limit: int = 100) -> list[RunState]:
        """WAITING runs whose timer ``fire_at`` <= ``now`` (timers to fire)."""
        ...

    async def stuck_runs(self, now: float, *, limit: int = 100) -> list[RunState]:
        """Non-terminal runs whose lease has expired (abandoned in-flight)."""
        ...

    async def list_runs(self) -> list[RunState]:
        """All runs (diagnostics / tests)."""
        ...


class InMemoryDurableStore:
    """A thread-safe, deep-copying in-memory :class:`DurableStore`.

    Deep-copies on the way in and out so a caller mutating its local
    :class:`RunState` can never corrupt the stored snapshot — the same isolation
    a real serialising backend gives for free. This is the test/dev backend; it
    is also a faithful model of the production contract.
    """

    __slots__ = ("_lock", "_runs")

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _clone(state: RunState) -> RunState:
        # model_copy(deep=True) gives a true round-trip-equivalent snapshot.
        return state.model_copy(deep=True)

    async def create(self, state: RunState) -> RunState:
        with self._lock:
            if state.run_id in self._runs:
                raise StoreConflictError(f"run {state.run_id!r} already exists")
            if state.revision != 0:
                raise StoreConflictError("a new run must start at revision 0")
            stored = self._clone(state)
            self._runs[stored.run_id] = stored
            logger.debug("saga.store.create", run_id=stored.run_id, workflow=stored.workflow)
            return self._clone(stored)

    async def load(self, run_id: str) -> RunState:
        state = await self.try_load(run_id)
        if state is None:
            raise RunNotFoundError(run_id)
        return state

    async def try_load(self, run_id: str) -> RunState | None:
        with self._lock:
            stored = self._runs.get(run_id)
            return self._clone(stored) if stored is not None else None

    async def save(self, state: RunState, *, expected_revision: int) -> RunState:
        with self._lock:
            current = self._runs.get(state.run_id)
            if current is None:
                raise RunNotFoundError(state.run_id)
            if current.revision != expected_revision:
                raise StoreConflictError(
                    f"run {state.run_id!r}: expected revision {expected_revision}, "
                    f"stored {current.revision}"
                )
            stored = self._clone(state)
            stored.revision = expected_revision + 1
            self._runs[stored.run_id] = stored
            logger.debug(
                "saga.store.save",
                run_id=stored.run_id,
                revision=stored.revision,
                status=stored.status,
            )
            return self._clone(stored)

    async def due_runs(self, now: float, *, limit: int = 100) -> list[RunState]:
        with self._lock:
            out: list[RunState] = []
            for s in self._runs.values():
                if (
                    s.status == RunStatus.WAITING
                    and s.timer is not None
                    and s.timer.fire_at <= now
                ):
                    out.append(self._clone(s))
                    if len(out) >= limit:
                        break
            return out

    async def stuck_runs(self, now: float, *, limit: int = 100) -> list[RunState]:
        with self._lock:
            out: list[RunState] = []
            for s in self._runs.values():
                if s.status in TERMINAL_RUN_STATUSES:
                    continue
                # WAITING runs are not "stuck" — they are legitimately parked
                # until their timer fires (handled by due_runs). A run is stuck
                # only if it holds an *expired lease* while RUNNING/COMPENSATING.
                if s.status == RunStatus.WAITING:
                    continue
                if s.lease_until is not None and s.lease_until <= now:
                    out.append(self._clone(s))
                    if len(out) >= limit:
                        break
            return out

    async def list_runs(self) -> list[RunState]:
        with self._lock:
            return [self._clone(s) for s in self._runs.values()]

    def snapshot(self) -> dict[str, dict[str, object]]:
        """A plain-dict snapshot of every run (test introspection)."""
        with self._lock:
            return {rid: copy.deepcopy(s.model_dump()) for rid, s in self._runs.items()}


__all__ = ["DurableStore", "InMemoryDurableStore"]
