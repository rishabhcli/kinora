"""DAG topology, cycle detection, kind-checking, and parallel batching.

Pure planning — no ffmpeg, no subprocess, no infra. These pin the structural
guarantees the engine relies on: a deterministic topological order, up-front cycle
/ dangling-input / kind-mismatch detection, and that mutually-independent branches
land in one parallel wave while a join waits for both upstreams.
"""

from __future__ import annotations

import pytest

from app.video.mediagraph.graph import (
    CycleError,
    DanglingInputError,
    DuplicateNodeError,
    DuplicateProducerError,
    KindMismatchError,
    MediaGraph,
)
from app.video.mediagraph.nodes import (
    CaptionBurnInNode,
    NormalizeNode,
    PosterNode,
    SourceNode,
    ThumbnailNode,
    WatermarkNode,
)
from app.video.mediagraph.types import ArtifactRef, MediaKind


def _source_graph() -> MediaGraph:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(NormalizeNode(node_id="normalize", source="source", out_name="master"))
    return g


# --------------------------------------------------------------------------- #
# Topological order
# --------------------------------------------------------------------------- #


def test_topological_order_is_dependency_respecting_and_deterministic() -> None:
    g = _source_graph()
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    g.add(PosterNode(node_id="poster", source="master", out_name="poster"))

    order = g.topological_order()
    # Producers precede consumers.
    assert order.index("source") < order.index("normalize")
    assert order.index("normalize") < order.index("thumb")
    assert order.index("normalize") < order.index("poster")
    # Deterministic: identical every call.
    assert g.topological_order() == order


def test_independent_branches_share_one_parallel_wave() -> None:
    g = _source_graph()
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    g.add(PosterNode(node_id="poster", source="master", out_name="poster"))

    batches = g.batches()
    # source | master | {thumb, poster}
    assert batches[0] == ["source"]
    assert batches[1] == ["normalize"]
    assert set(batches[2]) == {"poster", "thumb"}
    assert g.depth_of()["thumb"] == g.depth_of()["poster"] == 2


def test_join_node_waits_one_wave_past_both_inputs() -> None:
    g = _source_graph()
    g.declare_external(ArtifactRef(name="subs", kind=MediaKind.CAPTIONS, ext="vtt"))
    g.add(CaptionBurnInNode(node_id="burn", source="master", captions="subs", out_name="captioned"))
    depth = g.depth_of()
    # The captions external has no producing node, so the join depth is driven by
    # the master (depth 1) → join at depth 2.
    assert depth["burn"] == depth["normalize"] + 1


def test_roots_and_leaves() -> None:
    g = _source_graph()
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    assert g.roots() == ["source"]
    assert g.leaves() == ["thumb"]


# --------------------------------------------------------------------------- #
# Cycle / dangling / duplicate detection
# --------------------------------------------------------------------------- #


def test_cycle_is_detected() -> None:
    g = MediaGraph()
    g.add(NormalizeNode(node_id="a", source="b_out", out_name="a_out"))
    g.add(NormalizeNode(node_id="b", source="a_out", out_name="b_out"))
    with pytest.raises(CycleError):
        g.validate()


def test_dangling_input_is_detected() -> None:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(ThumbnailNode(node_id="t", source="missing", out_name="thumb"))
    with pytest.raises(DanglingInputError):
        g.validate()


def test_duplicate_producer_is_detected() -> None:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(NormalizeNode(node_id="a", source="source", out_name="dup"))
    g.add(NormalizeNode(node_id="b", source="source", out_name="dup"))
    with pytest.raises(DuplicateProducerError):
        g.validate()


def test_duplicate_node_id_is_rejected() -> None:
    g = MediaGraph()
    g.add(NormalizeNode(node_id="x", source="source", out_name="a"))
    with pytest.raises(DuplicateNodeError):
        g.add(NormalizeNode(node_id="x", source="source", out_name="b"))


# --------------------------------------------------------------------------- #
# Edge kind type-checking
# --------------------------------------------------------------------------- #


def test_captions_into_a_thumbnail_is_a_kind_mismatch() -> None:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="subs", kind=MediaKind.CAPTIONS, ext="vtt"))
    g.add(ThumbnailNode(node_id="t", source="subs", out_name="thumb"))
    with pytest.raises(KindMismatchError):
        g.validate()


def test_caption_burn_in_requires_captions_on_its_captions_input() -> None:
    g = _source_graph()
    # Feed a *video* where the captions input is expected → mismatch.
    g.add(
        CaptionBurnInNode(node_id="burn", source="master", captions="master", out_name="captioned")
    )
    with pytest.raises(KindMismatchError):
        g.validate()


def test_watermark_requires_image_on_its_mark_input() -> None:
    g = _source_graph()
    g.declare_external(ArtifactRef(name="subs", kind=MediaKind.CAPTIONS, ext="vtt"))
    g.add(WatermarkNode(node_id="wm", source="master", mark="subs", out_name="wmd"))
    with pytest.raises(KindMismatchError):
        g.validate()


def test_well_typed_join_validates() -> None:
    g = _source_graph()
    g.declare_external(ArtifactRef(name="subs", kind=MediaKind.CAPTIONS, ext="vtt"))
    g.declare_external(ArtifactRef(name="logo", kind=MediaKind.IMAGE, ext="png"))
    g.add(CaptionBurnInNode(node_id="burn", source="master", captions="subs"))
    g.add(WatermarkNode(node_id="wm", source="master", mark="logo"))
    g.validate()  # no raise


def test_edges_and_dependents_are_consistent() -> None:
    g = _source_graph()
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    edges = g.edges()
    deps = g.dependents()
    assert edges["thumb"] == {"normalize"}
    assert "thumb" in deps["normalize"]
    assert edges["normalize"] == {"source"}
