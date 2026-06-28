"""A render-graph DAG for parallel, dependency-ordered shot execution (§9.3/§9.6).

A scene is many shots, and most are *independent* — they can render in parallel
across the §4.9 committed lane. But two §9.3 render modes create a hard ordering:

* **video_continuation** extends *only* from a predecessor shot's QA-passed
  endpoint frame (§9.3) — so it must not start until that predecessor is accepted;
* the §9.6 continuation anchor (the last accepted frame → canon) chains shots in
  reading order within a scene.

This module models those as a directed acyclic graph of :class:`ShotNode`s with
explicit dependency edges, and a deterministic topological scheduler that yields
**ready-batches**: the set of shots whose dependencies are all satisfied, capped
to a concurrency limit. Independent shots fan out; a continuation chain stays
strictly ordered. Cycles and dangling dependencies are detected up front.

The graph is *pure planning* — it owns no provider, DB, or ffmpeg. Execution is
delegated to an injected async runner (the engine wires it to
``RenderPipeline.render_shot``), so the ordering logic is unit-testable with a
trivial fake runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agents.contracts import RenderMode
from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.observability import metrics

logger = get_logger("app.render.dag")


class NodeState(StrEnum):
    """A node's position in the scheduler's lifecycle (not the §9.7 shot state)."""

    PENDING = "pending"  # waiting on dependencies
    READY = "ready"  # all deps satisfied; eligible for a batch
    RUNNING = "running"  # dispatched to the runner
    DONE = "done"  # terminal — accepted/degraded (a successful sink)
    BLOCKED = "blocked"  # a dependency failed terminally → cannot run


@dataclass(slots=True)
class ShotNode:
    """One shot in the render graph.

    Attributes:
        shot_id / scene_id: identity.
        render_mode: the §9.3 mode (drives implicit continuation dependencies).
        depends_on: explicit predecessor shot ids that must be accepted first.
        order: reading-order index within the scene (a deterministic tiebreak).
        state: the scheduler lifecycle state.
        result: the runner's outcome status once executed (for dependents).
    """

    shot_id: str
    scene_id: str | None = None
    render_mode: RenderMode | None = None
    depends_on: set[str] = field(default_factory=set)
    order: int = 0
    state: NodeState = NodeState.PENDING
    result: ShotStatus | None = None

    @property
    def is_continuation(self) -> bool:
        """True when this shot extends a predecessor's accepted endpoint (§9.3)."""
        return self.render_mode is RenderMode.VIDEO_CONTINUATION


class CycleError(ValueError):
    """The dependency graph contains a cycle (not a DAG)."""


class DanglingDependencyError(ValueError):
    """A node depends on a shot id that is not in the graph."""


class RenderGraph:
    """A DAG of shot nodes with a deterministic topological batch scheduler."""

    def __init__(self) -> None:
        self._nodes: dict[str, ShotNode] = {}

    # -- construction -------------------------------------------------------- #

    def add(self, node: ShotNode) -> ShotNode:
        """Add a node (idempotent on shot_id; last write wins on attributes)."""
        self._nodes[node.shot_id] = node
        return node

    def add_shot(
        self,
        shot_id: str,
        *,
        scene_id: str | None = None,
        render_mode: RenderMode | None = None,
        depends_on: Iterable[str] | None = None,
        order: int = 0,
    ) -> ShotNode:
        """Convenience constructor for a node."""
        return self.add(
            ShotNode(
                shot_id=shot_id,
                scene_id=scene_id,
                render_mode=render_mode,
                depends_on=set(depends_on or ()),
                order=order,
            )
        )

    def link_continuation_chain(self, shot_ids: list[str]) -> None:
        """Make each ``video_continuation`` shot depend on its predecessor (§9.3).

        Given shots in reading order, a continuation shot is wired to depend on
        the immediately-preceding shot (the one whose accepted endpoint it
        extends). Non-continuation shots are left independent so they fan out.
        """
        for prev_id, cur_id in zip(shot_ids, shot_ids[1:], strict=False):
            node = self._nodes.get(cur_id)
            if node is not None and node.is_continuation:
                node.depends_on.add(prev_id)

    # -- introspection ------------------------------------------------------- #

    @property
    def nodes(self) -> dict[str, ShotNode]:
        return self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def validate(self) -> None:
        """Raise if a dependency dangles or the graph has a cycle (not a DAG)."""
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise DanglingDependencyError(f"{node.shot_id} depends on unknown {dep}")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        # Kahn's algorithm: a graph with a non-empty residue after removing all
        # zero-indegree nodes has a cycle.
        indeg = {sid: len(node.depends_on) for sid, node in self._nodes.items()}
        queue = [sid for sid, d in indeg.items() if d == 0]
        seen = 0
        dependents = self._dependents_index()
        while queue:
            sid = queue.pop()
            seen += 1
            for dep_sid in dependents.get(sid, ()):  # nodes that depend on sid
                indeg[dep_sid] -= 1
                if indeg[dep_sid] == 0:
                    queue.append(dep_sid)
        if seen != len(self._nodes):
            raise CycleError("render graph contains a dependency cycle")

    def _dependents_index(self) -> dict[str, list[str]]:
        """Reverse edges: dep_id → [shot_ids that depend on it]."""
        index: dict[str, list[str]] = {}
        for node in self._nodes.values():
            for dep in node.depends_on:
                index.setdefault(dep, []).append(node.shot_id)
        return index

    def topological_order(self) -> list[str]:
        """A deterministic topological order (ties broken by ``order`` then id)."""
        self.validate()
        indeg = {sid: len(node.depends_on) for sid, node in self._nodes.items()}
        dependents = self._dependents_index()
        order: list[str] = []
        ready = self._sorted([sid for sid, d in indeg.items() if d == 0])
        while ready:
            sid = ready.pop(0)
            order.append(sid)
            for dep_sid in dependents.get(sid, ()):
                indeg[dep_sid] -= 1
                if indeg[dep_sid] == 0:
                    ready.append(dep_sid)
            ready = self._sorted(ready)
        return order

    def _sorted(self, shot_ids: list[str]) -> list[str]:
        """Deterministic ordering of ready ids: by ``order`` then shot_id."""
        return sorted(set(shot_ids), key=lambda sid: (self._nodes[sid].order, sid))

    # -- batch scheduling ---------------------------------------------------- #

    def _deps_satisfied(self, node: ShotNode) -> bool:
        return all(
            self._nodes[dep].state is NodeState.DONE
            for dep in node.depends_on
            if dep in self._nodes
        )

    def _deps_blocked(self, node: ShotNode) -> bool:
        return any(
            self._nodes[dep].state is NodeState.BLOCKED
            or self._nodes[dep].result is ShotStatus.DEGRADED
            for dep in node.depends_on
            if dep in self._nodes
        )

    def ready_batch(self, *, max_parallel: int) -> list[ShotNode]:
        """The next deterministic ready-batch (≤ ``max_parallel``), advancing state.

        A pending node becomes ``READY`` once every dependency is ``DONE``. A
        continuation node whose predecessor only *degraded* (no accepted endpoint
        to extend) is marked ``BLOCKED`` — it cannot continuation-render, so the
        scheduler skips it for the caller to handle (degrade it directly). Returns
        the chosen nodes and marks them ``RUNNING``.
        """
        # Block continuation nodes whose predecessor cannot give an endpoint.
        for node in self._nodes.values():
            if (
                node.state is NodeState.PENDING
                and node.is_continuation
                and self._deps_blocked(node)
            ):
                node.state = NodeState.BLOCKED
        candidates = [
            node
            for node in self._nodes.values()
            if node.state is NodeState.PENDING and self._deps_satisfied(node)
        ]
        candidates.sort(key=lambda n: (n.order, n.shot_id))
        batch = candidates[: max(max_parallel, 1)]
        for node in batch:
            node.state = NodeState.RUNNING
        if batch:
            metrics.observe_dag_batch(len(batch))
            logger.info(
                "dag.ready_batch", size=len(batch), shots=[n.shot_id for n in batch]
            )
        return batch

    def mark_done(self, shot_id: str, result: ShotStatus) -> None:
        """Record a shot's terminal result so its dependents can advance."""
        node = self._nodes.get(shot_id)
        if node is None:
            return
        node.result = result
        node.state = NodeState.DONE

    @property
    def is_complete(self) -> bool:
        """True once every node is terminal (DONE or BLOCKED)."""
        return all(n.state in (NodeState.DONE, NodeState.BLOCKED) for n in self._nodes.values())

    @property
    def blocked(self) -> list[ShotNode]:
        """Nodes that cannot run because a dependency failed terminally."""
        return [n for n in self._nodes.values() if n.state is NodeState.BLOCKED]


# A runner renders one shot and returns its terminal status.
ShotRunner = Callable[[str], Awaitable[ShotStatus]]


@dataclass(slots=True)
class GraphRunReport:
    """The outcome of executing a whole graph."""

    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    batches: list[list[str]] = field(default_factory=list)
    results: dict[str, ShotStatus] = field(default_factory=dict)

    @property
    def batch_count(self) -> int:
        """How many parallel waves the scheduler released."""
        return len(self.batches)

    @property
    def max_parallelism(self) -> int:
        """The largest batch released (the realised render fan-out)."""
        return max((len(b) for b in self.batches), default=0)


async def run_graph(
    graph: RenderGraph, runner: ShotRunner, *, max_parallel: int = 4
) -> GraphRunReport:
    """Execute ``graph`` to completion, rendering each ready-batch concurrently.

    The scheduler releases successive ready-batches (≤ ``max_parallel``); the
    batch's shots render concurrently via the injected ``runner``; their results
    feed back into the graph so dependents unblock. Continuation shots whose
    predecessor degraded are reported ``blocked`` (the caller degrades them). The
    loop is deterministic given a deterministic runner.
    """
    graph.validate()
    report = GraphRunReport()
    while not graph.is_complete:
        batch = graph.ready_batch(max_parallel=max_parallel)
        if not batch:
            break  # nothing runnable left (the rest is blocked)
        report.batches.append([n.shot_id for n in batch])
        statuses = await asyncio.gather(*(runner(node.shot_id) for node in batch))
        for node, status in zip(batch, statuses, strict=True):
            graph.mark_done(node.shot_id, status)
            report.completed.append(node.shot_id)
            report.results[node.shot_id] = status
    report.blocked = [n.shot_id for n in graph.blocked]
    logger.info(
        "dag.run_done",
        completed=len(report.completed),
        blocked=len(report.blocked),
        batches=report.batch_count,
    )
    return report


def build_scene_graph(shots: list[dict[str, Any]]) -> RenderGraph:
    """Build a :class:`RenderGraph` from scene shot rows (reading-order chained).

    Each row is ``{shot_id, render_mode?, scene_id?, depends_on?}``; rows are taken
    in reading order (their list position is the ``order`` tiebreak) and the
    continuation chain is wired automatically so a ``video_continuation`` shot
    depends on its immediate predecessor's accepted endpoint (§9.3).
    """
    graph = RenderGraph()
    ids: list[str] = []
    for i, row in enumerate(shots):
        shot_id = str(row["shot_id"])
        mode_raw = row.get("render_mode")
        mode = RenderMode(mode_raw) if mode_raw else None
        graph.add_shot(
            shot_id,
            scene_id=row.get("scene_id"),
            render_mode=mode,
            depends_on=row.get("depends_on"),
            order=i,
        )
        ids.append(shot_id)
    graph.link_continuation_chain(ids)
    graph.validate()
    return graph


__all__ = [
    "CycleError",
    "DanglingDependencyError",
    "GraphRunReport",
    "NodeState",
    "RenderGraph",
    "ShotNode",
    "ShotRunner",
    "build_scene_graph",
    "run_graph",
]
