"""Up/down converters — a chain of migrators for graceful version negotiation.

A producer on schema ``v1`` and a consumer on ``v3`` should still talk. Rather than
require every consumer to handle every historical shape, we register *adjacent*
migrators — small functions that transform a payload across one version step — and
compose them into a chain. A consumer asks the
:class:`ConverterRegistry` to convert an incoming payload from its envelope version
to the version it handles; the registry finds the shortest step path and folds the
migrators.

Two directions:

* **upgrade** (``v_lo -> v_hi``): a newer consumer reads an older producer's body.
* **downgrade** (``v_hi -> v_lo``): an older consumer reads a newer producer's body.

A :class:`Migrator` declares its single step (``from_version -> to_version``,
``direction``) and a pure transform. The registry builds a directed graph over the
versions of a ``schema_id`` and runs a BFS for the shortest path, so a chain
``v1->v2->v3`` is discovered automatically from two adjacent migrators. When no path
exists it raises :class:`~app.servicemesh.errors.NoConversionPathError` — the signal
the consumer dispatcher turns into a dead-letter.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.servicemesh.errors import ConversionError, NoConversionPathError
from app.servicemesh.versioning import SemVer

__all__ = [
    "Direction",
    "Migrator",
    "ConverterRegistry",
    "Payload",
]

Payload = dict[str, Any]
TransformFn = Callable[[Payload], Payload]


class Direction(StrEnum):
    """Which way a migrator moves a payload along the version axis."""

    UP = "up"  # older -> newer (from_version < to_version)
    DOWN = "down"  # newer -> older (from_version > to_version)


@dataclass(frozen=True, slots=True)
class Migrator:
    """A single-step payload transform between two adjacent schema versions."""

    schema_id: str
    from_version: SemVer
    to_version: SemVer
    direction: Direction
    transform: TransformFn

    def __post_init__(self) -> None:
        if self.from_version == self.to_version:
            raise ConversionError(
                f"migrator for {self.schema_id} is a no-op ({self.from_version})"
            )
        going_up = self.from_version < self.to_version
        if going_up and self.direction is not Direction.UP:
            raise ConversionError(
                f"migrator {self.from_version}->{self.to_version} ascends but is "
                f"declared {self.direction.value}"
            )
        if not going_up and self.direction is not Direction.DOWN:
            raise ConversionError(
                f"migrator {self.from_version}->{self.to_version} descends but is "
                f"declared {self.direction.value}"
            )

    def apply(self, payload: Payload) -> Payload:
        """Run the transform on a *copy* so the caller's payload is never mutated."""
        try:
            return self.transform(dict(payload))
        except Exception as exc:  # noqa: BLE001 - normalize to the mesh taxonomy
            raise ConversionError(
                f"migrator {self.schema_id} {self.from_version}->{self.to_version} "
                f"failed: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ConversionPlan:
    """A resolved chain of migrators connecting two versions (for introspection)."""

    schema_id: str
    source: SemVer
    target: SemVer
    steps: tuple[Migrator, ...]

    @property
    def hops(self) -> int:
        return len(self.steps)


class ConverterRegistry:
    """Registers migrators and composes them into shortest-path conversion chains."""

    def __init__(self) -> None:
        # schema_id -> from_version -> {to_version: migrator}
        self._edges: dict[str, dict[SemVer, dict[SemVer, Migrator]]] = {}
        self._lock = threading.RLock()

    # -- registration ------------------------------------------------------- #
    def register(
        self,
        schema_id: str,
        from_version: SemVer | str,
        to_version: SemVer | str,
        transform: TransformFn,
    ) -> Migrator:
        """Register one adjacent-step migrator (direction inferred from versions)."""
        fv = SemVer.coerce(from_version)
        tv = SemVer.coerce(to_version)
        direction = Direction.UP if fv < tv else Direction.DOWN
        migrator = Migrator(
            schema_id=schema_id,
            from_version=fv,
            to_version=tv,
            direction=direction,
            transform=transform,
        )
        with self._lock:
            by_from = self._edges.setdefault(schema_id, {}).setdefault(fv, {})
            by_from[tv] = migrator
        return migrator

    def register_pair(
        self,
        schema_id: str,
        lower: SemVer | str,
        upper: SemVer | str,
        *,
        up: TransformFn,
        down: TransformFn,
    ) -> tuple[Migrator, Migrator]:
        """Register both directions between two adjacent versions in one call."""
        return (
            self.register(schema_id, lower, upper, up),
            self.register(schema_id, upper, lower, down),
        )

    # -- planning ----------------------------------------------------------- #
    def plan(
        self, schema_id: str, source: SemVer | str, target: SemVer | str
    ) -> ConversionPlan:
        """Find the shortest migrator chain ``source -> target`` (BFS) or raise."""
        src = SemVer.coerce(source)
        dst = SemVer.coerce(target)
        if src == dst:
            return ConversionPlan(schema_id, src, dst, ())

        with self._lock:
            graph = self._edges.get(schema_id, {})
            # BFS over version nodes; predecessors reconstruct the path.
            frontier: deque[SemVer] = deque([src])
            came_from: dict[SemVer, Migrator] = {}
            visited: set[SemVer] = {src}
            while frontier:
                node = frontier.popleft()
                if node == dst:
                    break
                for to_version, migrator in graph.get(node, {}).items():
                    if to_version not in visited:
                        visited.add(to_version)
                        came_from[to_version] = migrator
                        frontier.append(to_version)

        if dst not in came_from and src != dst:
            raise NoConversionPathError(
                f"no migrator chain for {schema_id}: {src} -> {dst}"
            )

        # Reconstruct.
        steps: list[Migrator] = []
        cursor = dst
        while cursor != src:
            migrator = came_from[cursor]
            steps.append(migrator)
            cursor = migrator.from_version
        steps.reverse()
        return ConversionPlan(schema_id, src, dst, tuple(steps))

    def can_convert(
        self, schema_id: str, source: SemVer | str, target: SemVer | str
    ) -> bool:
        """Whether a chain exists (does not raise)."""
        try:
            self.plan(schema_id, source, target)
            return True
        except NoConversionPathError:
            return False

    def has_any(self, schema_id: str) -> bool:
        """Whether *any* migrator is registered for ``schema_id``."""
        with self._lock:
            return bool(self._edges.get(schema_id))

    # -- execution ---------------------------------------------------------- #
    def convert(
        self,
        schema_id: str,
        payload: Payload,
        source: SemVer | str,
        target: SemVer | str,
    ) -> Payload:
        """Fold the shortest migrator chain over ``payload`` (``source -> target``)."""
        plan = self.plan(schema_id, source, target)
        result = dict(payload)
        for step in plan.steps:
            result = step.apply(result)
        return result
