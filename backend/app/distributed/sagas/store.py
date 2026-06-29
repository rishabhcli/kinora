"""The durable saga store — the crash-resume backbone.

A :class:`SagaStore` persists :class:`~app.distributed.sagas.types.SagaInstance`
records and their :class:`StepRecord`\\ s, and exposes the small set of *atomic*
operations the orchestrator needs:

* :meth:`start` — create an instance + its step rows **iff** no active instance
  already exists for the (definition, correlation_id) pair (the dedup that makes a
  re-delivered start safe). Returns the instance and whether it was newly created.
* :meth:`claim_due` — atomically lease the next runnable instance (one whose
  ``available_at`` has arrived) to a single worker via a lease token + deadline,
  so two orchestrators never drive the same saga concurrently.
* :meth:`save_instance` / :meth:`save_step` — persist a mutated instance/step
  (the orchestrator advances state in memory then flushes).
* :meth:`load` — rehydrate an instance + its steps for resume.
* :meth:`reap_expired` — return leased-but-lapsed instances to the runnable pool
  (this is what turns a crash mid-step into an automatic resume).
* read helpers (:meth:`get`, :meth:`list_instances`, :meth:`stats`).

:class:`InMemorySagaStore` is the reference implementation — durable *within* a
process, which is exactly what the deterministic virtual-clock crash-resume tests
need (a "crash" is dropping the orchestrator object while keeping the store; a
"resume" is constructing a fresh orchestrator over the same store). The Postgres
implementation lives in :mod:`app.distributed.sagas.db_store` behind this same
protocol.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from app.distributed.sagas.types import (
    TERMINAL_SAGA_STATUSES,
    SagaInstance,
    SagaStatus,
    StepRecord,
)


@dataclass(frozen=True, slots=True)
class StartResult:
    """Outcome of :meth:`SagaStore.start`."""

    instance: SagaInstance
    created: bool  # False => an active instance for this correlation id already existed


@dataclass(frozen=True, slots=True)
class LoadedSaga:
    """An instance plus its ordered step records (the full resume snapshot)."""

    instance: SagaInstance
    steps: list[StepRecord]


@dataclass(frozen=True, slots=True)
class SagaStats:
    """A point-in-time snapshot of instance counts by status + lifetime counters."""

    by_status: dict[str, int]
    started_total: int
    committed_total: int
    compensated_total: int
    failed_total: int

    @property
    def active(self) -> int:
        terminal = {s.value for s in TERMINAL_SAGA_STATUSES}
        return sum(c for s, c in self.by_status.items() if s not in terminal)


_ACTIVE = (
    SagaStatus.PENDING,
    SagaStatus.RUNNING,
    SagaStatus.COMPENSATING,
    SagaStatus.TIMED_OUT,
)


@runtime_checkable
class SagaStore(Protocol):
    """Durable, atomic persistence of saga instances + steps (the resume backbone)."""

    async def start(
        self,
        *,
        definition: str,
        correlation_id: str,
        steps: list[StepRecord],
        state: dict[str, Any] | None = None,
        deadline: datetime | None = None,
    ) -> StartResult: ...

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        definitions: list[str] | None = None,
    ) -> SagaInstance | None: ...

    async def load(self, saga_id: str) -> LoadedSaga | None: ...

    async def save_instance(self, instance: SagaInstance) -> None: ...

    async def save_step(self, step: StepRecord) -> None: ...

    async def get(self, saga_id: str) -> SagaInstance | None: ...

    async def list_instances(
        self,
        *,
        definition: str | None = None,
        status: SagaStatus | None = None,
        limit: int = 100,
    ) -> list[SagaInstance]: ...

    async def reap_expired(self, *, now: datetime) -> int: ...

    async def stats(self) -> SagaStats: ...


class InMemorySagaStore:
    """An in-process reference :class:`SagaStore` (the harness + crash-resume tests).

    Backed by dicts guarded by an :class:`asyncio.Lock`, so the atomic operations
    behave under concurrent orchestrators on one event loop. "Durable within a
    process" is precisely what the deterministic crash-resume tests want: the
    store survives the orchestrator being dropped and recreated.
    """

    def __init__(self) -> None:
        self._instances: dict[str, SagaInstance] = {}
        self._steps: dict[str, list[StepRecord]] = {}
        # (definition, correlation_id) -> instance id of the active instance.
        self._active_key: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._counters: dict[str, int] = {
            "started_total": 0,
            "committed_total": 0,
            "compensated_total": 0,
            "failed_total": 0,
        }

    async def start(
        self,
        *,
        definition: str,
        correlation_id: str,
        steps: list[StepRecord],
        state: dict[str, Any] | None = None,
        deadline: datetime | None = None,
    ) -> StartResult:
        async with self._lock:
            key = (definition, correlation_id)
            existing_id = self._active_key.get(key)
            if existing_id is not None:
                existing = self._instances.get(existing_id)
                if existing is not None and not existing.is_terminal:
                    return StartResult(instance=_copy_instance(existing), created=False)
            saga_id = uuid.uuid4().hex
            instance = SagaInstance(
                id=saga_id,
                definition=definition,
                correlation_id=correlation_id,
                status=SagaStatus.PENDING,
                cursor=0,
                state=dict(state or {}),
                deadline=deadline,
            )
            self._instances[saga_id] = instance
            self._steps[saga_id] = [replace(s, saga_id=saga_id) for s in steps]
            self._active_key[key] = saga_id
            self._counters["started_total"] += 1
            return StartResult(instance=_copy_instance(instance), created=True)

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        definitions: list[str] | None = None,
    ) -> SagaInstance | None:
        names = set(definitions) if definitions is not None else None
        async with self._lock:
            candidates = [
                inst
                for inst in self._instances.values()
                if inst.status in _ACTIVE
                and (inst.available_at is None or inst.available_at <= now)
                and (inst.lease_until is None or inst.lease_until <= now)
                and (names is None or inst.definition in names)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda i: (i.available_at or i.created_at or now, i.id))
            inst = candidates[0]
            inst.lease_token = uuid.uuid4().hex
            inst.lease_until = now + timedelta(seconds=lease_seconds)
            return _copy_instance(inst)

    async def load(self, saga_id: str) -> LoadedSaga | None:
        async with self._lock:
            inst = self._instances.get(saga_id)
            if inst is None:
                return None
            steps = [_copy_step(s) for s in self._steps.get(saga_id, [])]
            return LoadedSaga(instance=_copy_instance(inst), steps=steps)

    async def save_instance(self, instance: SagaInstance) -> None:
        async with self._lock:
            stored = self._instances.get(instance.id)
            if stored is None:
                return
            became_terminal = not stored.is_terminal and instance.is_terminal
            self._instances[instance.id] = _copy_instance(instance)
            if became_terminal:
                self._on_terminal(instance)

    async def save_step(self, step: StepRecord) -> None:
        async with self._lock:
            steps = self._steps.get(step.saga_id)
            if steps is None:
                return
            for i, s in enumerate(steps):
                if s.index == step.index:
                    steps[i] = _copy_step(step)
                    return
            steps.append(_copy_step(step))

    async def get(self, saga_id: str) -> SagaInstance | None:
        async with self._lock:
            inst = self._instances.get(saga_id)
            return _copy_instance(inst) if inst is not None else None

    async def list_instances(
        self,
        *,
        definition: str | None = None,
        status: SagaStatus | None = None,
        limit: int = 100,
    ) -> list[SagaInstance]:
        async with self._lock:
            out = [
                _copy_instance(i)
                for i in self._instances.values()
                if (definition is None or i.definition == definition)
                and (status is None or i.status == status)
            ]
        out.sort(key=lambda i: i.created_at or datetime.min.replace(tzinfo=None), reverse=True)
        return out[:limit]

    async def reap_expired(self, *, now: datetime) -> int:
        async with self._lock:
            count = 0
            for inst in self._instances.values():
                if (
                    inst.status in _ACTIVE
                    and inst.lease_until is not None
                    and inst.lease_until <= now
                ):
                    inst.lease_token = None
                    inst.lease_until = None
                    inst.available_at = now
                    count += 1
            return count

    async def stats(self) -> SagaStats:
        async with self._lock:
            by_status: dict[str, int] = {}
            for inst in self._instances.values():
                by_status[inst.status.value] = by_status.get(inst.status.value, 0) + 1
            return SagaStats(
                by_status=by_status,
                started_total=self._counters["started_total"],
                committed_total=self._counters["committed_total"],
                compensated_total=self._counters["compensated_total"],
                failed_total=self._counters["failed_total"],
            )

    def _on_terminal(self, instance: SagaInstance) -> None:
        key = (instance.definition, instance.correlation_id)
        if self._active_key.get(key) == instance.id:
            del self._active_key[key]
        if instance.status is SagaStatus.COMPLETED:
            self._counters["committed_total"] += 1
        elif instance.status is SagaStatus.COMPENSATED:
            self._counters["compensated_total"] += 1
        elif instance.status is SagaStatus.FAILED:
            self._counters["failed_total"] += 1


def _copy_instance(inst: SagaInstance) -> SagaInstance:
    return replace(inst, state=dict(inst.state))


def _copy_step(step: StepRecord) -> StepRecord:
    return replace(step, output=dict(step.output))


__all__ = [
    "InMemorySagaStore",
    "LoadedSaga",
    "SagaStats",
    "SagaStore",
    "StartResult",
]
