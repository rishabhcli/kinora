"""The media-transform DAG — nodes wired by artifact name, with topo scheduling.

A :class:`MediaGraph` holds :class:`~app.video.mediagraph.nodes.TransformNode`\\ s.
Edges are *implicit*: a node's declared input names are resolved to the producing
node (the one whose outputs include an artifact of that name). The graph:

* resolves producers and detects dangling inputs (an input no node produces) and
  duplicate producers (two nodes claiming the same artifact name);
* validates the edge **kinds** (a thumbnail must be fed a video/image, never a
  captions file) before anything runs;
* detects cycles (Kahn's algorithm) and yields a deterministic topological order;
* releases deterministic **ready-batches** so independent branches fan out in
  parallel while a join (caption burn-in, watermark) waits for both upstreams.

This module is *pure planning* — it owns no provider, DB, or ffmpeg. The
:mod:`app.video.mediagraph.engine` executes a validated graph over an injectable
runner. Mirrors the design of :mod:`app.render.dag` but schedules media
transforms, not shots.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.video.mediagraph.nodes import TransformNode
from app.video.mediagraph.types import ArtifactRef, MediaKind

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class GraphError(ValueError):
    """Base class for media-graph construction/validation errors."""


class CycleError(GraphError):
    """The transform graph contains a dependency cycle (not a DAG)."""


class DanglingInputError(GraphError):
    """A node declares an input artifact that no node produces and which was not
    supplied as an external (source) artifact."""


class DuplicateProducerError(GraphError):
    """Two nodes both produce an artifact with the same logical name."""


class DuplicateNodeError(GraphError):
    """Two nodes share a ``node_id``."""


class KindMismatchError(GraphError):
    """An edge connects an artifact of one kind to an input expecting another."""


# --------------------------------------------------------------------------- #
# Which input kinds each consuming node accepts
# --------------------------------------------------------------------------- #

#: For each node kind, the set of artifact kinds it may legally consume *per
#: input position*. Caption burn-in and watermark are joins with two heterogenous
#: inputs, so they are validated structurally (see :meth:`_validate_kinds`).
_VIDEO_OR_IMAGE = frozenset({MediaKind.VIDEO, MediaKind.IMAGE})


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


class MediaGraph:
    """A DAG of media-transform nodes wired by produced/consumed artifact name."""

    def __init__(self) -> None:
        self._nodes: dict[str, TransformNode] = {}
        #: External artifacts supplied at run time (the source clip, a captions
        #: file, a watermark image) — inputs that resolve to no producing node.
        self._external: dict[str, ArtifactRef] = {}

    # -- construction ------------------------------------------------------- #

    def add(self, node: TransformNode) -> TransformNode:
        """Add a node. Raises on a duplicate ``node_id``."""
        if node.node_id in self._nodes:
            raise DuplicateNodeError(f"duplicate node_id {node.node_id!r}")
        self._nodes[node.node_id] = node
        return node

    def add_all(self, nodes: Iterable[TransformNode]) -> MediaGraph:
        for node in nodes:
            self.add(node)
        return self

    def declare_external(self, ref: ArtifactRef) -> ArtifactRef:
        """Declare an externally-supplied artifact (e.g. the source clip).

        Inputs that resolve to a declared external are satisfied at run time by the
        caller rather than produced by a node, so they are not dangling.
        """
        self._external[ref.name] = ref
        return ref

    # -- introspection ------------------------------------------------------ #

    @property
    def nodes(self) -> dict[str, TransformNode]:
        return dict(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def node(self, node_id: str) -> TransformNode:
        return self._nodes[node_id]

    def producers(self) -> dict[str, str]:
        """Artifact name → producing node_id (validated to be unique)."""
        index: dict[str, str] = {}
        for nid, node in self._nodes.items():
            for out in node.outputs:
                if out.name in index:
                    raise DuplicateProducerError(
                        f"artifact {out.name!r} produced by both "
                        f"{index[out.name]!r} and {nid!r}"
                    )
                index[out.name] = nid
        return index

    def _output_refs(self) -> dict[str, ArtifactRef]:
        """Artifact name → its :class:`ArtifactRef` (across all nodes + externals)."""
        refs: dict[str, ArtifactRef] = dict(self._external)
        for node in self._nodes.values():
            for out in node.outputs:
                refs[out.name] = out
        return refs

    def edges(self) -> dict[str, set[str]]:
        """node_id → set of upstream node_ids it depends on (its producers)."""
        producers = self.producers()
        deps: dict[str, set[str]] = {nid: set() for nid in self._nodes}
        for nid, node in self._nodes.items():
            for name in node.input_names:
                producer = producers.get(name)
                if producer is not None:
                    deps[nid].add(producer)
        return deps

    def dependents(self) -> dict[str, set[str]]:
        """Reverse edges: node_id → set of node_ids that depend on it."""
        rev: dict[str, set[str]] = defaultdict(set)
        for nid, deps in self.edges().items():
            for dep in deps:
                rev[dep].add(nid)
        return {nid: rev.get(nid, set()) for nid in self._nodes}

    # -- validation --------------------------------------------------------- #

    def validate(self) -> None:
        """Full structural validation: producers, dangling inputs, kinds, acyclic.

        Raises a :class:`GraphError` subclass on the first violation. Cheap and
        side-effect-free — the engine calls it before executing.
        """
        producers = self.producers()  # also raises DuplicateProducerError
        self._validate_inputs_resolve(producers)
        self._validate_kinds()
        self._assert_acyclic()

    def _validate_inputs_resolve(self, producers: dict[str, str]) -> None:
        known = set(producers) | set(self._external)
        for nid, node in self._nodes.items():
            for name in node.input_names:
                if name not in known:
                    raise DanglingInputError(
                        f"{nid!r} consumes {name!r}, which no node produces "
                        f"and which was not declared external"
                    )

    def _validate_kinds(self) -> None:
        refs = self._output_refs()
        for nid, node in self._nodes.items():
            for name in node.input_names:
                got = refs[name].kind
                expected = self._expected_kinds(node, name)
                if expected is not None and got not in expected:
                    raise KindMismatchError(
                        f"{nid!r} input {name!r} expects one of "
                        f"{sorted(k.value for k in expected)} but got {got.value!r}"
                    )

    def _expected_kinds(self, node: TransformNode, input_name: str) -> frozenset[MediaKind] | None:
        """Which artifact kinds an input position legally accepts (None = any)."""
        # Imported lazily to keep this module free of node subclass imports at
        # the top (avoids any import-order coupling); the checks are structural.
        from app.video.mediagraph.nodes import (
            CaptionBurnInNode,
            LoudnessNormalizeNode,
            WatermarkNode,
        )

        if isinstance(node, CaptionBurnInNode):
            if input_name == node.captions:
                return frozenset({MediaKind.CAPTIONS})
            return frozenset({MediaKind.VIDEO})
        if isinstance(node, WatermarkNode):
            if input_name == node.mark:
                return frozenset({MediaKind.IMAGE})
            return frozenset({MediaKind.VIDEO})
        if isinstance(node, LoudnessNormalizeNode):
            return frozenset({MediaKind.VIDEO, MediaKind.AUDIO})
        # probe/normalize/thumbnail/poster/gif/sprite/extract: video or image.
        return _VIDEO_OR_IMAGE

    def _assert_acyclic(self) -> None:
        """Kahn's algorithm — a non-empty residue after peeling zero-indegree
        nodes means a cycle."""
        deps = self.edges()
        dependents = self.dependents()
        indeg = {nid: len(deps[nid]) for nid in self._nodes}
        queue = [nid for nid, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            nid = queue.pop()
            seen += 1
            for child in dependents[nid]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if seen != len(self._nodes):
            cyclic = sorted(nid for nid, d in indeg.items() if d > 0)
            raise CycleError(f"media graph contains a cycle among {cyclic}")

    # -- ordering ----------------------------------------------------------- #

    def topological_order(self) -> list[str]:
        """A deterministic topological order (ties broken by node_id).

        Validates first, so a malformed graph raises rather than returning a
        partial order.
        """
        self.validate()
        deps = self.edges()
        dependents = self.dependents()
        indeg = {nid: len(deps[nid]) for nid in self._nodes}
        ready = sorted(nid for nid, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for child in sorted(dependents[nid]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
            ready.sort()
        return order

    def depth_of(self) -> dict[str, int]:
        """node_id → longest-path depth from a root (its scheduling wave index)."""
        deps = self.edges()
        depth: dict[str, int] = {}
        for nid in self.topological_order():
            parents = deps[nid]
            depth[nid] = 0 if not parents else 1 + max(depth[p] for p in parents)
        return depth

    def batches(self) -> list[list[str]]:
        """Deterministic parallel waves: each wave's nodes are mutually independent.

        Wave *k* is every node whose longest-path depth is *k*; all of them have
        their upstreams satisfied by waves ``< k`` and none depend on each other,
        so they may run concurrently. Independent branches (thumbnail / poster /
        gif / sprite off the same master) land in the same wave; a join lands one
        wave after both its inputs.
        """
        depth = self.depth_of()
        waves: dict[int, list[str]] = defaultdict(list)
        for nid, d in depth.items():
            waves[d].append(nid)
        return [sorted(waves[d]) for d in sorted(waves)]

    def roots(self) -> list[str]:
        """Nodes with no upstream dependency (their inputs are all external)."""
        deps = self.edges()
        return sorted(nid for nid, d in deps.items() if not d)

    def leaves(self) -> list[str]:
        """Nodes nothing depends on (the terminal derivatives)."""
        dependents = self.dependents()
        return sorted(nid for nid, d in dependents.items() if not d)


__all__ = [
    "CycleError",
    "DanglingInputError",
    "DuplicateNodeError",
    "DuplicateProducerError",
    "GraphError",
    "KindMismatchError",
    "MediaGraph",
]
