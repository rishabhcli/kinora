"""A universal media transform graph for a finished clip's derived media.

A finished Kinora clip needs a fixed family of *derived* media — a normalised
master, a thumbnail, a poster, a preview GIF, a scrubbing sprite-sheet, a
loudness-normalised master, burned-in captions, a watermark — regardless of which
model (Wan, MiniMax, the Ken-Burns degradation lane) produced the source. This
package models that derivation as a **declarative DAG of media transforms** and
runs it with parallelism, content-hash caching, and failure isolation.

Layers (each independently testable):

* :mod:`~app.video.mediagraph.types` — self-owned domain types (artifacts,
  ffmpeg-invocation plans, results) + the ``content_hash`` cache primitive.
* :mod:`~app.video.mediagraph.nodes` — the transform-node catalogue; each node
  declares its inputs/outputs and a **pure ffmpeg arg-plan**.
* :mod:`~app.video.mediagraph.graph` — the DAG: producer resolution, kind-checked
  edges, cycle detection, topological order, deterministic parallel waves.
* :mod:`~app.video.mediagraph.plan` — the **pure plan layer**: the exact ordered
  invocations + intermediate-artifact graph (fully unit-testable, no ffmpeg).
* :mod:`~app.video.mediagraph.cache` — per-node content-hash caching (skip
  already-produced derivatives; idempotent re-runs).
* :mod:`~app.video.mediagraph.runner` — the injectable runner (real subprocess /
  deterministic fake).
* :mod:`~app.video.mediagraph.engine` — the executor: topo scheduling, parallel
  branches, caching, failure isolation + partial results.
* :mod:`~app.video.mediagraph.presets` — ready-made graphs (the standard
  derived-media set).

FINAL-round note: this subsystem is self-contained under its own namespace and
imports nothing from the unmerged round-1/round-2 ``normalize`` / ``delivery``
packages — every type it needs is owned here.
"""

from __future__ import annotations

from app.video.mediagraph.cache import (
    CacheStore,
    FileSystemCacheStore,
    InMemoryCacheStore,
    NullCacheStore,
    node_cache_key,
)
from app.video.mediagraph.engine import MediaGraphEngine
from app.video.mediagraph.graph import (
    CycleError,
    DanglingInputError,
    DuplicateNodeError,
    DuplicateProducerError,
    GraphError,
    KindMismatchError,
    MediaGraph,
)
from app.video.mediagraph.nodes import (
    CaptionBurnInNode,
    ExtractLastFrameNode,
    LoudnessNormalizeNode,
    NormalizeNode,
    PlanContext,
    PosterNode,
    PreviewGifNode,
    ProbeNode,
    ScrubbingSpriteSheetNode,
    SourceNode,
    ThumbnailNode,
    TransformNode,
    WatermarkCorner,
    WatermarkNode,
)
from app.video.mediagraph.plan import ExecutionPlan, PlannedNode, build_plan
from app.video.mediagraph.presets import DerivativesSpec, build_derivatives_graph
from app.video.mediagraph.runner import (
    BinaryUnavailableError,
    FakeRunner,
    Runner,
    SubprocessRunner,
    ffmpeg_available,
)
from app.video.mediagraph.types import (
    FILM_GEOMETRY,
    Artifact,
    ArtifactRef,
    FfmpegInvocation,
    Geometry,
    GraphResult,
    MediaKind,
    NodeKind,
    NodeResult,
    NodeStatus,
    RunResult,
    content_hash,
    hash_bytes,
    hash_file,
)

__all__ = [
    "FILM_GEOMETRY",
    "Artifact",
    "ArtifactRef",
    "BinaryUnavailableError",
    "CacheStore",
    "CaptionBurnInNode",
    "CycleError",
    "DanglingInputError",
    "DerivativesSpec",
    "DuplicateNodeError",
    "DuplicateProducerError",
    "ExecutionPlan",
    "ExtractLastFrameNode",
    "FakeRunner",
    "FfmpegInvocation",
    "FileSystemCacheStore",
    "Geometry",
    "GraphError",
    "GraphResult",
    "InMemoryCacheStore",
    "KindMismatchError",
    "LoudnessNormalizeNode",
    "MediaGraph",
    "MediaGraphEngine",
    "MediaKind",
    "NodeKind",
    "NodeResult",
    "NodeStatus",
    "NormalizeNode",
    "NullCacheStore",
    "PlanContext",
    "PlannedNode",
    "PosterNode",
    "PreviewGifNode",
    "ProbeNode",
    "Runner",
    "RunResult",
    "ScrubbingSpriteSheetNode",
    "SourceNode",
    "SubprocessRunner",
    "ThumbnailNode",
    "TransformNode",
    "WatermarkCorner",
    "WatermarkNode",
    "build_derivatives_graph",
    "build_plan",
    "content_hash",
    "ffmpeg_available",
    "hash_bytes",
    "hash_file",
    "node_cache_key",
]
