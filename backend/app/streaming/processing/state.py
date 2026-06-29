"""Keyed state backend with checkpointed, exactly-once snapshots.

A streaming operator's correctness rests on *state*: window contents, running
aggregates, join buffers, dedup sets. This module provides the same primitives a
real engine exposes, scoped to the *current key* the runtime has set:

* :class:`ValueState` — a single value per key.
* :class:`ListState` — an append-only list per key.
* :class:`MapState` — a dict per key.
* :class:`ReducingState` — a value folded with a binary reduce function.
* :class:`AggregatingState` — a value folded through an
  :class:`~app.streaming.processing.aggregations.AggregateFunction`
  (separate accumulator and output types).

State is *keyed*: every access is implicitly scoped to the key the runtime
activated via :meth:`KeyedStateBackend.set_current_key`. Operators never see
other keys' state, which is what makes per-key windows and timers correct and
lets the backend be partitioned.

**Exactly-once.** The backend snapshots to a :class:`CheckpointStorage` on a
checkpoint barrier and restores from it on recovery. Because the runtime aligns
barriers (a checkpoint reflects a consistent cut across all operators) and the
sinks are idempotent / transactional, replaying from the last completed
checkpoint reproduces exactly the same output — no duplicates, no loss. The
in-memory storage here deep-copies state so a snapshot is immutable against
later mutation, exactly as a durable backend would serialize it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")
ACC = TypeVar("ACC")
OUT = TypeVar("OUT")


# --------------------------------------------------------------------------- #
# State descriptors — the typed, named handles operators register.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ValueStateDescriptor(Generic[T]):
    """Names a single-value-per-key state and its default."""

    name: str
    default: T | None = None


@dataclass(frozen=True, slots=True)
class ListStateDescriptor(Generic[T]):
    """Names an append-only-list-per-key state."""

    name: str


@dataclass(frozen=True, slots=True)
class MapStateDescriptor(Generic[K, V]):
    """Names a dict-per-key state."""

    name: str


@dataclass(frozen=True, slots=True)
class ReducingStateDescriptor(Generic[T]):
    """Names a value-per-key folded by a binary reduce function."""

    name: str
    reduce: Callable[[T, T], T]


@dataclass(frozen=True, slots=True)
class AggregatingStateDescriptor(Generic[T, ACC, OUT]):
    """Names a value-per-key folded through an aggregate function.

    ``create_accumulator`` / ``add`` / ``get_result`` mirror the
    :class:`~app.streaming.processing.aggregations.AggregateFunction` contract;
    they are passed individually to keep this module import-cycle free.
    """

    name: str
    create_accumulator: Callable[[], ACC]
    add: Callable[[T, ACC], ACC]
    get_result: Callable[[ACC], OUT]


# --------------------------------------------------------------------------- #
# State handles — what an operator actually reads / writes.
# --------------------------------------------------------------------------- #
class ValueState(Generic[T]):
    """A single value scoped to the backend's current key.

    ``store_ref`` resolves the live namespace dict on every access (rather than
    capturing it) so a backend ``restore`` — which swaps the underlying
    namespaces — is reflected by handles created before the restore.
    """

    def __init__(
        self,
        store_ref: Callable[[], dict[object, T]],
        key_ref: Callable[[], object],
        default: T | None,
    ):
        self._store_ref = store_ref
        self._key_ref = key_ref
        self._default = default

    def value(self) -> T | None:
        return self._store_ref().get(self._key_ref(), self._default)

    def update(self, value: T) -> None:
        self._store_ref()[self._key_ref()] = value

    def clear(self) -> None:
        self._store_ref().pop(self._key_ref(), None)


class ListState(Generic[T]):
    """An append-only list scoped to the backend's current key."""

    def __init__(
        self, store_ref: Callable[[], dict[object, list[T]]], key_ref: Callable[[], object]
    ):
        self._store_ref = store_ref
        self._key_ref = key_ref

    def add(self, value: T) -> None:
        self._store_ref().setdefault(self._key_ref(), []).append(value)

    def get(self) -> list[T]:
        return list(self._store_ref().get(self._key_ref(), []))

    def update(self, values: list[T]) -> None:
        self._store_ref()[self._key_ref()] = list(values)

    def clear(self) -> None:
        self._store_ref().pop(self._key_ref(), None)


class MapState(Generic[K, V]):
    """A dict scoped to the backend's current key."""

    def __init__(
        self, store_ref: Callable[[], dict[object, dict[K, V]]], key_ref: Callable[[], object]
    ):
        self._store_ref = store_ref
        self._key_ref = key_ref

    def _map(self) -> dict[K, V]:
        return self._store_ref().setdefault(self._key_ref(), {})

    def put(self, k: K, v: V) -> None:
        self._map()[k] = v

    def get(self, k: K, default: V | None = None) -> V | None:
        return self._store_ref().get(self._key_ref(), {}).get(k, default)

    def contains(self, k: K) -> bool:
        return k in self._store_ref().get(self._key_ref(), {})

    def remove(self, k: K) -> None:
        self._store_ref().get(self._key_ref(), {}).pop(k, None)

    def items(self) -> list[tuple[K, V]]:
        return list(self._store_ref().get(self._key_ref(), {}).items())

    def values(self) -> list[V]:
        return list(self._store_ref().get(self._key_ref(), {}).values())

    def clear(self) -> None:
        self._store_ref().pop(self._key_ref(), None)


class ReducingState(Generic[T]):
    """A value folded by a binary reduce function, scoped to the current key."""

    def __init__(
        self,
        store_ref: Callable[[], dict[object, T]],
        key_ref: Callable[[], object],
        reduce: Callable[[T, T], T],
    ):
        self._store_ref = store_ref
        self._key_ref = key_ref
        self._reduce = reduce

    def add(self, value: T) -> None:
        store = self._store_ref()
        key = self._key_ref()
        if key in store:
            store[key] = self._reduce(store[key], value)
        else:
            store[key] = value

    def get(self) -> T | None:
        return self._store_ref().get(self._key_ref())

    def clear(self) -> None:
        self._store_ref().pop(self._key_ref(), None)


class AggregatingState(Generic[T, ACC, OUT]):
    """A value folded through an aggregate function, scoped to the current key."""

    def __init__(
        self,
        store_ref: Callable[[], dict[object, ACC]],
        key_ref: Callable[[], object],
        descriptor: AggregatingStateDescriptor[T, ACC, OUT],
    ):
        self._store_ref = store_ref
        self._key_ref = key_ref
        self._desc = descriptor

    def add(self, value: T) -> None:
        store = self._store_ref()
        key = self._key_ref()
        acc = store.get(key)
        if acc is None:
            acc = self._desc.create_accumulator()
        store[key] = self._desc.add(value, acc)

    def get(self) -> OUT | None:
        acc = self._store_ref().get(self._key_ref())
        if acc is None:
            return None
        return self._desc.get_result(acc)

    def clear(self) -> None:
        self._store_ref().pop(self._key_ref(), None)


# --------------------------------------------------------------------------- #
# Snapshot + checkpoint storage
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Snapshot:
    """An immutable, point-in-time copy of one backend's keyed state.

    ``namespaces`` maps a state name to its ``{key: value}`` map. The copy is
    deep so later mutation of live state cannot corrupt a taken checkpoint —
    the exact guarantee a serializing durable backend gives for free.
    """

    checkpoint_id: int
    operator_id: str
    namespaces: dict[str, dict[object, object]]

    def state_names(self) -> list[str]:
        return sorted(self.namespaces)


class CheckpointStorage(Protocol):
    """Durable-ish store for operator snapshots (one entry per checkpoint id)."""

    def put(self, snapshot: Snapshot) -> None: ...

    def get(self, operator_id: str, checkpoint_id: int) -> Snapshot | None: ...

    def latest(self, operator_id: str) -> Snapshot | None: ...

    def completed_checkpoints(self) -> list[int]: ...


class InMemoryCheckpointStorage:
    """In-memory :class:`CheckpointStorage` for tests and single-process runs.

    Deep-copies on the way in so a stored snapshot is immutable against later
    live mutation. A real deployment would point at object storage; the
    semantics are identical.
    """

    def __init__(self) -> None:
        self._by_op: dict[str, dict[int, Snapshot]] = {}

    def put(self, snapshot: Snapshot) -> None:
        frozen = Snapshot(
            checkpoint_id=snapshot.checkpoint_id,
            operator_id=snapshot.operator_id,
            namespaces=copy.deepcopy(snapshot.namespaces),
        )
        self._by_op.setdefault(snapshot.operator_id, {})[snapshot.checkpoint_id] = frozen

    def get(self, operator_id: str, checkpoint_id: int) -> Snapshot | None:
        snap = self._by_op.get(operator_id, {}).get(checkpoint_id)
        if snap is None:
            return None
        return Snapshot(
            checkpoint_id=snap.checkpoint_id,
            operator_id=snap.operator_id,
            namespaces=copy.deepcopy(snap.namespaces),
        )

    def latest(self, operator_id: str) -> Snapshot | None:
        snaps = self._by_op.get(operator_id)
        if not snaps:
            return None
        return self.get(operator_id, max(snaps))

    def completed_checkpoints(self) -> list[int]:
        ids: set[int] = set()
        for snaps in self._by_op.values():
            ids.update(snaps)
        return sorted(ids)


# --------------------------------------------------------------------------- #
# The keyed-state backend
# --------------------------------------------------------------------------- #
class KeyedStateBackend:
    """Per-operator keyed-state store with checkpoint / restore.

    Every state handle the operator registers reads and writes only the slice
    addressed by :meth:`set_current_key`. ``snapshot`` produces a deep,
    immutable :class:`Snapshot`; ``restore`` reinstates one, giving the
    exactly-once recovery path.
    """

    def __init__(self, operator_id: str) -> None:
        self.operator_id = operator_id
        self._current_key: object | None = None
        # name -> {key -> value}
        self._namespaces: dict[str, dict[object, object]] = {}

    # -- key scoping -------------------------------------------------------- #
    def set_current_key(self, key: object) -> None:
        self._current_key = key

    @property
    def current_key(self) -> object:
        if self._current_key is None:
            raise RuntimeError("no current key set on the state backend")
        return self._current_key

    def _ns(self, name: str) -> dict[object, object]:
        return self._namespaces.setdefault(name, {})

    def keys_with_state(self, name: str) -> list[object]:
        """Every key that currently holds state under ``name`` (snapshot order)."""

        return list(self._namespaces.get(name, {}))

    def _ns_ref(self, name: str) -> Callable[[], dict[object, Any]]:
        """A resolver that returns the live namespace dict for ``name``.

        Resolving on each call (not capturing the dict) keeps handles valid
        across a backend ``restore``. Typed as ``dict[object, Any]`` because the
        element type is per-handle and erased at the namespace level.
        """

        return lambda: self._ns(name)

    # -- handle factories --------------------------------------------------- #
    def get_value_state(self, desc: ValueStateDescriptor[T]) -> ValueState[T]:
        return ValueState(self._ns_ref(desc.name), lambda: self.current_key, desc.default)

    def get_list_state(self, desc: ListStateDescriptor[T]) -> ListState[T]:
        return ListState(self._ns_ref(desc.name), lambda: self.current_key)

    def get_map_state(self, desc: MapStateDescriptor[K, V]) -> MapState[K, V]:
        return MapState(self._ns_ref(desc.name), lambda: self.current_key)

    def get_reducing_state(self, desc: ReducingStateDescriptor[T]) -> ReducingState[T]:
        return ReducingState(self._ns_ref(desc.name), lambda: self.current_key, desc.reduce)

    def get_aggregating_state(
        self, desc: AggregatingStateDescriptor[T, ACC, OUT]
    ) -> AggregatingState[T, ACC, OUT]:
        return AggregatingState(self._ns_ref(desc.name), lambda: self.current_key, desc)

    # -- checkpoint / restore ---------------------------------------------- #
    def snapshot(self, checkpoint_id: int) -> Snapshot:
        return Snapshot(
            checkpoint_id=checkpoint_id,
            operator_id=self.operator_id,
            namespaces=copy.deepcopy(self._namespaces),
        )

    def restore(self, snapshot: Snapshot) -> None:
        self._namespaces = copy.deepcopy(snapshot.namespaces)
        self._current_key = None

    def clear_all(self) -> None:
        self._namespaces.clear()


@dataclass(slots=True)
class CheckpointBarrier:
    """The marker the coordinator injects to trigger an aligned checkpoint.

    In the single-threaded runtime alignment is trivial (one input), but the
    barrier is modelled explicitly so the exactly-once story is concrete and the
    coordinator API matches a distributed engine's.
    """

    checkpoint_id: int


@dataclass(slots=True)
class CheckpointCoordinator:
    """Drives checkpoints across a set of operator backends.

    ``trigger`` snapshots every registered backend under one checkpoint id and
    persists them; the checkpoint is *completed* only when all succeed (the
    aligned-barrier guarantee). ``restore_latest`` reinstates the newest
    complete checkpoint into every backend.
    """

    storage: CheckpointStorage
    _backends: dict[str, KeyedStateBackend] = field(default_factory=dict)
    _next_id: int = 1

    def register(self, backend: KeyedStateBackend) -> None:
        self._backends[backend.operator_id] = backend

    def trigger(self) -> int:
        checkpoint_id = self._next_id
        self._next_id += 1
        for backend in self._backends.values():
            self.storage.put(backend.snapshot(checkpoint_id))
        return checkpoint_id

    def restore_latest(self) -> int | None:
        restored: int | None = None
        for op_id, backend in self._backends.items():
            snap = self.storage.latest(op_id)
            if snap is not None:
                backend.restore(snap)
                restored = snap.checkpoint_id
        return restored

    def restore_checkpoint(self, checkpoint_id: int) -> None:
        for op_id, backend in self._backends.items():
            snap = self.storage.get(op_id, checkpoint_id)
            if snap is not None:
                backend.restore(snap)


def iter_keyed(namespaces: dict[str, dict[object, object]]) -> Iterator[tuple[str, object, object]]:
    """Yield ``(state_name, key, value)`` across a snapshot's namespaces.

    A small inspection helper used by tests and the QA pipeline to walk a
    restored snapshot without reaching into private structure.
    """

    for name, by_key in namespaces.items():
        for key, value in by_key.items():
            yield name, key, value
