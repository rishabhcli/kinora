"""Content-hash node cache keys + store implementations (no ffmpeg).

Pins the cache primitive: a node's key folds in the source content, the node's
knobs, and its upstream hashes, so a changed clip / knob / upstream invalidates
the key, while identical inputs reproduce it (the basis of idempotent re-runs).
"""

from __future__ import annotations

from pathlib import Path

from app.video.mediagraph.cache import (
    FileSystemCacheStore,
    InMemoryCacheStore,
    NullCacheStore,
    node_cache_key,
)
from app.video.mediagraph.nodes import NormalizeNode, ThumbnailNode
from app.video.mediagraph.types import (
    Artifact,
    ArtifactRef,
    Geometry,
    MediaKind,
    content_hash,
    hash_bytes,
)

# --------------------------------------------------------------------------- #
# content_hash primitive
# --------------------------------------------------------------------------- #


def test_content_hash_is_deterministic_and_order_sensitive() -> None:
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("b", "a")
    # The record separator prevents ("a","b") colliding with ("ab",).
    assert content_hash("a", "b") != content_hash("ab")


def test_content_hash_canonicalises_mapping_key_order() -> None:
    assert content_hash({"x": 1, "y": 2}) == content_hash({"y": 2, "x": 1})
    assert content_hash({"x": 1}) != content_hash({"x": 2})


def test_content_hash_distinguishes_types() -> None:
    assert content_hash(1) != content_hash("1")
    assert content_hash(True) != content_hash(1)
    assert content_hash(None) != content_hash("")


# --------------------------------------------------------------------------- #
# node_cache_key
# --------------------------------------------------------------------------- #


def test_node_key_changes_with_source_hash() -> None:
    node = NormalizeNode(node_id="n", source="source", out_name="master")
    k1 = node_cache_key(node, source_hash="aaa")
    k2 = node_cache_key(node, source_hash="bbb")
    assert k1 != k2


def test_node_key_changes_with_node_knobs() -> None:
    a = NormalizeNode(node_id="n", source="source", out_name="m", fps=30)
    b = NormalizeNode(node_id="n", source="source", out_name="m", fps=24)
    assert node_cache_key(a, source_hash="x") != node_cache_key(b, source_hash="x")


def test_node_key_changes_with_upstream_hash() -> None:
    node = ThumbnailNode(node_id="t", source="master", out_name="thumb")
    k1 = node_cache_key(node, source_hash="s", upstream_hashes=["u1"])
    k2 = node_cache_key(node, source_hash="s", upstream_hashes=["u2"])
    assert k1 != k2


def test_node_key_is_stable_for_identical_inputs() -> None:
    node = ThumbnailNode(node_id="t", source="master", geometry=Geometry(width=10, height=20))
    key = node_cache_key(node, source_hash="s", upstream_hashes=["u"])
    again = node_cache_key(
        ThumbnailNode(node_id="t", source="master", geometry=Geometry(width=10, height=20)),
        source_hash="s",
        upstream_hashes=["u"],
    )
    assert key == again


def test_node_id_does_not_affect_the_cache_key() -> None:
    # Two nodes with the same transform but different ids should share a key
    # (the produced bytes are identical) so dedupe works across graph positions.
    a = NormalizeNode(node_id="a", source="source", out_name="m")
    b = NormalizeNode(node_id="b", source="source", out_name="m")
    assert node_cache_key(a, source_hash="s") == node_cache_key(b, source_hash="s")


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #


def _artifact(name: str, path: Path) -> Artifact:
    return Artifact(
        ref=ArtifactRef(name=name, kind=MediaKind.VIDEO, ext="mp4"),
        path=path,
        sha256=hash_bytes(b"x"),
        size_bytes=1,
    )


def test_in_memory_store_roundtrip() -> None:
    store = InMemoryCacheStore()
    assert store.get("k") is None
    assert store.has("k") is False
    arts = (_artifact("master", Path("/m.mp4")),)
    store.put("k", arts)
    assert store.has("k") is True
    assert store.get("k") == arts
    assert len(store) == 1


def test_null_store_never_hits() -> None:
    store = NullCacheStore()
    store.put("k", (_artifact("m", Path("/m.mp4")),))
    assert store.get("k") is None
    assert store.has("k") is False


def test_filesystem_store_roundtrip(tmp_path: Path) -> None:
    media = tmp_path / "master.mp4"
    media.write_bytes(b"video")
    store = FileSystemCacheStore(tmp_path / "cache")
    art = Artifact(
        ref=ArtifactRef(name="master", kind=MediaKind.VIDEO, ext="mp4"),
        path=media,
        sha256=hash_bytes(b"video"),
        size_bytes=5,
        meta={"duration_s": 3.0},
    )
    store.put("key1", (art,))
    got = store.get("key1")
    assert got is not None
    assert len(got) == 1
    assert got[0].name == "master"
    assert got[0].path == media
    assert got[0].meta == {"duration_s": 3.0}
    assert store.has("key1") is True


def test_filesystem_store_misses_when_artifact_was_pruned(tmp_path: Path) -> None:
    media = tmp_path / "gone.mp4"
    media.write_bytes(b"x")
    store = FileSystemCacheStore(tmp_path / "cache")
    store.put("k", (_artifact("gone", media),))
    media.unlink()  # the working dir was cleaned
    # A vanished artifact must invalidate the entry rather than replay a ghost.
    assert store.get("k") is None
    assert store.has("k") is False
