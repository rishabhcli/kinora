"""The DAG executor — runs a media graph over an injectable runner.

Given a :class:`~app.video.mediagraph.graph.MediaGraph`, a source clip, and the
external inputs, the engine:

* builds the deterministic :class:`~app.video.mediagraph.plan.ExecutionPlan`;
* schedules nodes in **topological waves**, running each wave's mutually-
  independent nodes **concurrently** (independent branches fan out; a join waits
  for both upstreams);
* applies **per-node content-hash caching** — a node whose ``(source, signature,
  upstream-hashes)`` key is already in the store is *skipped* and its artifacts
  replayed, so a re-run is **idempotent** and does no ffmpeg work;
* **isolates failures** — when a node's invocation fails (or it is skipped because
  an upstream failed), only the sub-tree that *depends on it* is skipped; sibling
  branches still produce their derivatives (the **partial-result** contract: a
  broken sprite-sheet must not deny the reader a thumbnail).

The engine touches the filesystem (it writes derived media) and a runner (it runs
ffmpeg), so its full pipeline is exercised by ffmpeg-gated integration tests, but
its scheduling / caching / failure-isolation logic is exercised deterministically
with the :class:`~app.video.mediagraph.runner.FakeRunner`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from app.core.logging import get_logger
from app.observability import metrics
from app.video.mediagraph.cache import CacheStore, NullCacheStore, node_cache_key
from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import ScrubbingSpriteSheetNode, TransformNode
from app.video.mediagraph.plan import PlannedNode, build_plan
from app.video.mediagraph.runner import Runner
from app.video.mediagraph.types import (
    Artifact,
    GraphResult,
    NodeResult,
    NodeStatus,
    hash_bytes,
    hash_file,
)

logger = get_logger("app.video.mediagraph.engine")


class MediaGraphEngine:
    """Executes a media graph with topo scheduling, caching, and failure isolation."""

    def __init__(
        self,
        *,
        runner: Runner,
        cache: CacheStore | None = None,
        max_parallel: int = 4,
    ) -> None:
        self._runner = runner
        # NB: an empty InMemoryCacheStore is falsy (``__len__``==0), so test for
        # ``None`` explicitly rather than truthiness — ``cache or NullCacheStore()``
        # would silently discard a freshly-constructed cache.
        self._cache = cache if cache is not None else NullCacheStore()
        self._max_parallel = max(1, max_parallel)

    async def execute(
        self,
        graph: MediaGraph,
        *,
        work_dir: Path | str,
        external_paths: Mapping[str, Path | str],
        source_input: str = "source",
    ) -> GraphResult:
        """Derive every artifact in ``graph`` from the supplied external inputs.

        Args:
            graph: a graph (validated during planning).
            work_dir: where derived media is written.
            external_paths: artifact name → resolved source path for every declared
                external (the source clip, a captions file, a watermark image).
            source_input: which external is the *source clip* whose content hash
                seeds every node's cache key (defaults to ``"source"``).

        Returns:
            A :class:`GraphResult` with per-node outcomes and an artifact index.
            Failed/skipped nodes leave the rest intact (partial results).
        """
        root = Path(work_dir)
        root.mkdir(parents=True, exist_ok=True)
        plan = build_plan(graph, work_dir=root, external_paths=external_paths)

        source_path = plan.external_paths.get(source_input)
        source_hash = (
            hash_file(source_path)
            if source_path is not None and source_path.exists()
            else _hash_str(source_input)
        )

        planned_by_id = {p.node_id: p for p in plan.ordered}
        edges = graph.edges()

        results: dict[str, NodeResult] = {}
        # node_id → the content hash it ran/cached under (feeds children's keys).
        node_hash: dict[str, str] = {}

        # Cap concurrent in-flight nodes (and thus ffmpeg subprocesses) so a wide
        # wave does not oversubscribe the host. Independent branches still fan out.
        sem = asyncio.Semaphore(self._max_parallel)

        async def _guarded(nid: str) -> NodeResult:
            async with sem:
                return await self._run_node(
                    planned_by_id[nid],
                    source_hash=source_hash,
                    upstream_results={d: results[d] for d in edges[nid] if d in results},
                    upstream_hashes={d: node_hash.get(d, "") for d in edges[nid]},
                )

        for wave in plan.batches:
            # Run a wave's nodes concurrently (bounded by the semaphore); each node
            # in a wave is independent of the others.
            wave_results = await asyncio.gather(*(_guarded(nid) for nid in wave))
            for nid, result in zip(wave, wave_results, strict=True):
                results[nid] = result
                node_hash[nid] = result.content_hash

        graph_result = GraphResult(results=results, batches=plan.batches)
        logger.info(
            "mediagraph.execute.done",
            nodes=len(results),
            succeeded=len(graph_result.succeeded),
            cached=len(graph_result.cached),
            failed=len(graph_result.failed),
            skipped=len(graph_result.skipped),
            partial=graph_result.partial,
        )
        return graph_result

    # -- per-node execution ------------------------------------------------- #

    async def _run_node(
        self,
        planned: PlannedNode,
        *,
        source_hash: str,
        upstream_results: Mapping[str, NodeResult],
        upstream_hashes: Mapping[str, str],
    ) -> NodeResult:
        node = planned.node
        nid = planned.node_id

        # Failure isolation: if any upstream did not make its outputs available,
        # this node cannot run — skip it (and, transitively, its dependents).
        broken = [d for d, r in upstream_results.items() if not r.status.is_ok]
        if broken:
            return NodeResult(
                node_id=nid,
                status=NodeStatus.SKIPPED,
                error=f"upstream not available: {sorted(broken)}",
            )

        key = node_cache_key(
            node,
            source_hash=source_hash,
            upstream_hashes=[upstream_hashes[d] for d in sorted(upstream_hashes)],
        )

        # Cache hit → replay artifacts, run no ffmpeg (idempotent re-run).
        cached = self._cache.get(key)
        if cached is not None:
            metrics.inc_cache(hit=True)
            logger.info("mediagraph.node.cached", node=nid, key=key[:12])
            return NodeResult(
                node_id=nid,
                status=NodeStatus.CACHED,
                artifacts=cached,
                content_hash=key,
            )
        metrics.inc_cache(hit=False)

        # A node with no invocations (the source leaf) is trivially available.
        if not planned.invocations:
            return NodeResult(node_id=nid, status=NodeStatus.SUCCEEDED, content_hash=key)

        # Run the node's invocations in order (a node's own steps are sequential —
        # e.g. the GIF's palettegen → paletteuse). Stop on the first failure.
        probe_stdout: dict[str, str] = {}
        for inv in planned.invocations:
            run_result = await self._runner.run(inv)
            if not run_result.ok:
                logger.warning(
                    "mediagraph.node.failed", node=nid, label=inv.label, rc=run_result.returncode
                )
                return NodeResult(
                    node_id=nid,
                    status=NodeStatus.FAILED,
                    error=f"{inv.label or inv.binary}: rc={run_result.returncode} "
                    f"{run_result.stderr[-300:]}",
                    content_hash=key,
                    invocations=len(planned.invocations),
                )
            if inv.captures_stdout and inv.produces is not None:
                probe_stdout[inv.produces.name] = run_result.stdout

        # Side-effect outputs not written by ffmpeg (probe JSON, sprite manifest).
        self._write_sidecars(node, planned, probe_stdout)

        artifacts = self._materialise_outputs(planned)
        self._cache.put(key, artifacts)
        return NodeResult(
            node_id=nid,
            status=NodeStatus.SUCCEEDED,
            artifacts=artifacts,
            content_hash=key,
            invocations=len(planned.invocations),
        )

    # -- output materialisation -------------------------------------------- #

    def _write_sidecars(
        self,
        node: TransformNode,
        planned: PlannedNode,
        probe_stdout: Mapping[str, str],
    ) -> None:
        """Write the non-ffmpeg side-effect outputs (probe JSON, sprite manifest)."""
        # Probe captures: the captured stdout JSON *is* the artifact's bytes.
        for name, stdout in probe_stdout.items():
            path = planned.output_paths.get(name)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(stdout or "{}", "utf-8")
        # Sprite-sheet manifest sidecar (pure data, computed by the node).
        if isinstance(node, ScrubbingSpriteSheetNode):
            manifest_path = planned.output_paths.get(node.sidecar_name)
            if manifest_path is not None:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(node.manifest(), indent=2), "utf-8")

    def _materialise_outputs(self, planned: PlannedNode) -> tuple[Artifact, ...]:
        """Build :class:`Artifact`\\ s for each declared output that now exists."""
        artifacts: list[Artifact] = []
        for out in planned.node.outputs:
            path = planned.output_paths[out.name]
            if not path.exists():
                # An output the node was expected to produce is missing — surface
                # it as an empty-but-located artifact rather than crash, so a
                # partial run still records what *was* produced. (The node already
                # reported SUCCEEDED only if every invocation's rc==0.)
                artifacts.append(Artifact(ref=out, path=path))
                continue
            size = path.stat().st_size
            artifacts.append(
                Artifact(
                    ref=out,
                    path=path,
                    sha256=hash_file(path) if size else "",
                    size_bytes=size,
                )
            )
        return tuple(artifacts)


def _hash_str(value: str) -> str:
    return hash_bytes(value.encode("utf-8"))


__all__ = [
    "MediaGraphEngine",
]
