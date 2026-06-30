"""The pure PLAN layer — the exact ordered ffmpeg invocations + artifact graph.

Given a validated :class:`~app.video.mediagraph.graph.MediaGraph`, an
:class:`ExecutionPlan` enumerates — without running anything — every node's
:class:`~app.video.mediagraph.types.FfmpegInvocation` in topological order, the
artifact each invocation produces, and how the intermediate artifacts wire one
node to the next. It is *fully deterministic*: planned against a fixed working
directory and a fixed map of external (source) paths, the argument vectors are
byte-for-byte identical run to run, so the entire plan is unit-testable with no
ffmpeg, subprocess, provider, DB, or network.

The executor (:mod:`app.video.mediagraph.engine`) consumes this plan; tests
assert on it directly (exact arg-plan per node, intermediate-artifact graph).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import PlanContext, TransformNode
from app.video.mediagraph.types import FfmpegInvocation

# --------------------------------------------------------------------------- #
# Planned node — a node's resolved invocations + the artifacts it produces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlannedNode:
    """One node's place in the plan: its inputs, outputs, and arg-plan."""

    node_id: str
    node: TransformNode
    #: Input artifact name → resolved on-disk path it reads from.
    input_paths: dict[str, Path]
    #: Output artifact name → resolved on-disk path it writes to.
    output_paths: dict[str, Path]
    #: The ordered ffmpeg/ffprobe invocations producing this node's outputs.
    invocations: tuple[FfmpegInvocation, ...]
    #: Upstream node_ids this node consumes outputs from (intermediate edges).
    upstreams: tuple[str, ...]

    @property
    def commands(self) -> list[tuple[str, ...]]:
        """The full logical command vectors (binary + args), in order."""
        return [inv.command() for inv in self.invocations]


# --------------------------------------------------------------------------- #
# Execution plan — the whole graph, resolved & topologically ordered
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """The fully-resolved, deterministic plan for an entire media graph."""

    #: Planned nodes in topological order.
    ordered: tuple[PlannedNode, ...]
    #: Deterministic parallel waves (node_ids), each wave mutually-independent.
    batches: tuple[tuple[str, ...], ...]
    #: The working directory every output path is rooted under.
    work_dir: Path
    #: External (caller-supplied) artifact name → its resolved source path.
    external_paths: dict[str, Path] = field(default_factory=dict)

    def by_id(self, node_id: str) -> PlannedNode:
        for planned in self.ordered:
            if planned.node_id == node_id:
                return planned
        raise KeyError(node_id)

    @property
    def invocations(self) -> list[FfmpegInvocation]:
        """Every invocation across the whole plan, in topological order."""
        out: list[FfmpegInvocation] = []
        for planned in self.ordered:
            out.extend(planned.invocations)
        return out

    def artifact_paths(self) -> dict[str, Path]:
        """Every produced artifact name → its resolved on-disk path."""
        index: dict[str, Path] = dict(self.external_paths)
        for planned in self.ordered:
            index.update(planned.output_paths)
        return index

    def intermediate_graph(self) -> dict[str, tuple[str, ...]]:
        """node_id → the upstream node_ids it consumes (the artifact wiring)."""
        return {p.node_id: p.upstreams for p in self.ordered}

    def explain(self) -> str:
        """A human-readable rendering of the whole plan (logs / debugging)."""
        lines: list[str] = [f"ExecutionPlan(work_dir={self.work_dir}):"]
        for wave_i, wave in enumerate(self.batches):
            lines.append(f"  wave {wave_i}: {', '.join(wave)}")
        for planned in self.ordered:
            up = ", ".join(planned.upstreams) or "-"
            lines.append(f"  [{planned.node_id}] <- {up}")
            for inv in planned.invocations:
                lines.append(f"      {inv.binary} {' '.join(inv.args)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #


def build_plan(
    graph: MediaGraph,
    *,
    work_dir: Path | str,
    external_paths: Mapping[str, Path | str] | None = None,
) -> ExecutionPlan:
    """Resolve ``graph`` into a deterministic :class:`ExecutionPlan`.

    Args:
        graph: a graph (validated here before planning).
        work_dir: the directory every node writes its outputs under. Each node
            also reads its inputs from the path the producing node wrote (or from
            ``external_paths`` for a source/captions/watermark input).
        external_paths: artifact name → resolved source path for every declared
            external artifact. Inputs that resolve to a node output are wired
            automatically.

    Returns:
        A plan whose invocations are byte-for-byte deterministic given the same
        inputs.

    Raises:
        GraphError: if the graph is malformed (dangling input, cycle, …).
        KeyError: if a declared external artifact has no supplied path.
    """
    graph.validate()
    root = Path(work_dir)
    externals: dict[str, Path] = {k: Path(v) for k, v in (external_paths or {}).items()}

    producers = graph.producers()
    # The resolved path for every artifact name (filled as we walk topo order).
    resolved: dict[str, Path] = dict(externals)

    ordered: list[PlannedNode] = []
    edges = graph.edges()
    for node_id in graph.topological_order():
        node = graph.node(node_id)
        # Resolve this node's input paths from already-resolved artifacts.
        input_paths: dict[str, Path] = {}
        for name in node.input_names:
            if name not in resolved:
                # Producer exists (validation passed) but external path missing.
                raise KeyError(
                    f"input {name!r} for node {node_id!r} is unresolved; "
                    f"declare it via external_paths"
                    if name not in producers
                    else f"internal: producer of {name!r} not yet planned"
                )
            input_paths[name] = resolved[name]

        ctx = PlanContext(inputs=input_paths, out_dir=root)
        invocations = node.build_invocations(ctx)

        # Resolve + register this node's output paths. An output whose name is a
        # declared external (the SourceNode, which re-exposes the supplied clip and
        # writes nothing) resolves to the external path rather than a synthetic
        # work-dir path — so downstream nodes read the *actual* source on disk.
        output_paths: dict[str, Path] = {}
        for out in node.outputs:
            path = externals[out.name] if out.name in externals else root / out.filename
            output_paths[out.name] = path
            resolved[out.name] = path

        ordered.append(
            PlannedNode(
                node_id=node_id,
                node=node,
                input_paths=input_paths,
                output_paths=output_paths,
                invocations=invocations,
                upstreams=tuple(sorted(edges[node_id])),
            )
        )

    batches = tuple(tuple(wave) for wave in graph.batches())
    return ExecutionPlan(
        ordered=tuple(ordered),
        batches=batches,
        work_dir=root,
        external_paths=externals,
    )


__all__ = [
    "ExecutionPlan",
    "PlannedNode",
    "build_plan",
]
