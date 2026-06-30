"""The DAG executor over a FakeRunner — scheduling, caching, failure isolation.

Deterministic and ffmpeg-free: the :class:`FakeRunner` records the exact commands,
writes placeholder outputs so downstream hashing works, and can force chosen nodes
to fail. These pin the engine's contracts: parallel waves, content-hash skip,
idempotent re-runs, failure isolation, and partial results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video.mediagraph.cache import InMemoryCacheStore, NullCacheStore
from app.video.mediagraph.engine import MediaGraphEngine
from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import (
    NormalizeNode,
    PosterNode,
    ProbeNode,
    SourceNode,
    ThumbnailNode,
)
from app.video.mediagraph.presets import DerivativesSpec, build_derivatives_graph
from app.video.mediagraph.runner import FakeRunner
from app.video.mediagraph.types import ArtifactRef, MediaKind, NodeStatus

pytestmark = pytest.mark.asyncio


def _fan_out_graph() -> MediaGraph:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(NormalizeNode(node_id="normalize", source="source", out_name="master"))
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    g.add(PosterNode(node_id="poster", source="master", out_name="poster"))
    g.add(ProbeNode(node_id="probe", source="source", out_name="probe"))
    g.validate()
    return g


def _write_source(tmp_path: Path) -> Path:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"SOURCE-BYTES")
    return src


# --------------------------------------------------------------------------- #
# Happy path + parallel batching
# --------------------------------------------------------------------------- #


async def test_executes_every_node_and_indexes_artifacts(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    engine = MediaGraphEngine(runner=FakeRunner(), cache=InMemoryCacheStore())
    result = await engine.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert result.ok is True
    assert result.partial is False
    assert set(result.succeeded) == {"source", "normalize", "thumb", "poster", "probe"}
    arts = result.artifacts()
    assert {"master", "thumb", "poster", "probe"} <= set(arts)
    # Real placeholder files were written and hashed.
    assert arts["thumb"].path.exists()
    assert arts["thumb"].sha256 != ""


async def test_batches_reflect_parallel_waves(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    engine = MediaGraphEngine(runner=FakeRunner(), cache=NullCacheStore())
    result = await engine.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert result.batches[0] == ("source",)
    assert set(result.batches[1]) == {"normalize", "probe"}
    assert set(result.batches[2]) == {"thumb", "poster"}
    assert result.max_parallelism == 2


# --------------------------------------------------------------------------- #
# Content-hash caching / idempotent re-runs
# --------------------------------------------------------------------------- #


async def test_second_run_is_a_cache_hit_with_no_ffmpeg(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    cache = InMemoryCacheStore()

    r1_runner = FakeRunner()
    e1 = MediaGraphEngine(runner=r1_runner, cache=cache)
    res1 = await e1.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert len(res1.succeeded) == 5
    assert r1_runner.calls  # ffmpeg ran the first time

    r2_runner = FakeRunner()
    e2 = MediaGraphEngine(runner=r2_runner, cache=cache)
    res2 = await e2.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    # Every node with invocations is replayed from cache; no ffmpeg runs.
    assert set(res2.cached) == {"normalize", "thumb", "poster", "probe"}
    assert r2_runner.calls == []
    assert res2.ok is True


async def test_changed_source_busts_the_cache(tmp_path: Path) -> None:
    g = _fan_out_graph()
    cache = InMemoryCacheStore()
    src = _write_source(tmp_path)
    e1 = MediaGraphEngine(runner=FakeRunner(), cache=cache)
    await e1.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})

    # Different clip bytes → different source hash → no cache hit.
    src.write_bytes(b"DIFFERENT-CLIP")
    runner = FakeRunner()
    e2 = MediaGraphEngine(runner=runner, cache=cache)
    res = await e2.execute(g, work_dir=tmp_path / "out2", external_paths={"source": src})
    assert res.cached == []
    assert runner.calls  # had to re-run ffmpeg


# --------------------------------------------------------------------------- #
# Failure isolation + partial results
# --------------------------------------------------------------------------- #


async def test_failed_leaf_does_not_sink_siblings(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    runner = FakeRunner(fail_labels={"thumb:thumbnail"})
    engine = MediaGraphEngine(runner=runner, cache=NullCacheStore())
    res = await engine.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert res.failed == ["thumb"]
    assert res.partial is True
    # Sibling derivatives still produced their outputs.
    assert res.results["poster"].status is NodeStatus.SUCCEEDED
    assert res.results["probe"].status is NodeStatus.SUCCEEDED
    assert res.artifact("poster") is not None


async def test_failed_upstream_skips_whole_subtree(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    runner = FakeRunner(fail_labels={"normalize:normalize"})
    engine = MediaGraphEngine(runner=runner, cache=NullCacheStore())
    res = await engine.execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert res.failed == ["normalize"]
    # Everything derived from the master is skipped (not failed).
    assert res.results["thumb"].status is NodeStatus.SKIPPED
    assert res.results["poster"].status is NodeStatus.SKIPPED
    # But probe (off the source, not the master) still succeeds → partial result.
    assert res.results["probe"].status is NodeStatus.SUCCEEDED
    assert res.partial is True


async def test_skip_reason_names_the_broken_upstream(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    runner = FakeRunner(fail_labels={"normalize:normalize"})
    res = await MediaGraphEngine(runner=runner, cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.results["thumb"].error is not None
    assert "normalize" in res.results["thumb"].error


# --------------------------------------------------------------------------- #
# The standard derivatives preset, end to end (fake)
# --------------------------------------------------------------------------- #


async def test_full_derivatives_preset_with_joins(tmp_path: Path) -> None:
    g = build_derivatives_graph(DerivativesSpec(captions_input="captions", watermark_input="logo"))
    src = _write_source(tmp_path)
    subs = tmp_path / "subs.vtt"
    subs.write_text("WEBVTT\n", "utf-8")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"PNG")
    engine = MediaGraphEngine(runner=FakeRunner(), cache=InMemoryCacheStore())
    res = await engine.execute(
        g,
        work_dir=tmp_path / "out",
        external_paths={"source": src, "captions": subs, "logo": logo},
    )
    assert res.ok is True
    arts = res.artifacts()
    expected = {
        "master",
        "thumb",
        "poster",
        "last_frame",
        "preview",
        "sprite",
        "sprite_manifest",
        "loudnorm",
        "probe",
        "captioned",
        "watermarked",
    }
    assert expected <= set(arts)
    # The sprite manifest sidecar is real JSON written by the engine.
    import json

    manifest = json.loads(arts["sprite_manifest"].path.read_text("utf-8"))
    assert manifest["tile_count"] == 25


async def test_probe_sidecar_captures_runner_stdout(tmp_path: Path) -> None:
    g = _fan_out_graph()
    src = _write_source(tmp_path)
    runner = FakeRunner(probe_stdout='{"format":{"duration":"3.0"}}')
    res = await MediaGraphEngine(runner=runner, cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    probe_path = res.require_artifact("probe").path
    assert probe_path.read_text("utf-8") == '{"format":{"duration":"3.0"}}'


async def test_max_parallel_caps_concurrent_nodes(tmp_path: Path) -> None:
    # A wide fan-out with max_parallel=1 still completes (serialised), proving the
    # semaphore does not deadlock and the result is independent of the cap.
    g = build_derivatives_graph(DerivativesSpec(probe=True))
    src = _write_source(tmp_path)
    res = await MediaGraphEngine(
        runner=FakeRunner(), cache=NullCacheStore(), max_parallel=1
    ).execute(g, work_dir=tmp_path / "out", external_paths={"source": src})
    assert res.ok is True
    assert len(res.succeeded) == len(g)
