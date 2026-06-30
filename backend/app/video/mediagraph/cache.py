"""Per-node content-hash caching — skip derivatives that already exist.

Deriving the full media set for a clip is expensive (several ffmpeg passes), and
the same clip is processed repeatedly (re-runs after a partial failure, idempotent
retries, re-deriving one missing artifact). Each node is keyed by a **content
hash** that folds in:

* the source clip's content hash (so a different clip never replays a cache hit),
* the node's deterministic signature (its knobs — geometry, fps, crf, … —
  anything that changes the bytes it emits),
* every upstream node's content hash (so a changed master invalidates everything
  derived from it).

If a store already holds the artifacts for that key, the node is **skipped** and
its artifacts are replayed; otherwise it runs and its outputs are recorded. This
makes a re-run **idempotent** — the second pass produces the same bytes and does
no ffmpeg work.

The :class:`CacheStore` protocol is injectable; an :class:`InMemoryCacheStore`
(tests) and a :class:`FileSystemCacheStore` (a real on-disk manifest) ship here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.video.mediagraph.nodes import TransformNode
from app.video.mediagraph.types import Artifact, ArtifactRef, content_hash

# --------------------------------------------------------------------------- #
# The cache key
# --------------------------------------------------------------------------- #


def node_cache_key(
    node: TransformNode,
    *,
    source_hash: str,
    upstream_hashes: Sequence[str] = (),
) -> str:
    """The content-hash cache key for ``node`` given its upstreams' hashes.

    Deterministic and order-sensitive: same source + same node knobs + same
    upstream content ⇒ same key ⇒ a cache hit on re-run.
    """
    return content_hash(
        "mediagraph.v1",
        source_hash,
        node.signature(),
        tuple(upstream_hashes),
    )


# --------------------------------------------------------------------------- #
# A cached entry — the artifacts a node produced under a given key
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Store protocol + implementations
# --------------------------------------------------------------------------- #


@runtime_checkable
class CacheStore(Protocol):
    """An injectable store of node-keyed produced artifacts."""

    def get(self, key: str) -> tuple[Artifact, ...] | None:
        """The artifacts recorded under ``key``, or ``None`` for a miss."""
        ...

    def put(self, key: str, artifacts: Sequence[Artifact]) -> None:
        """Record the artifacts a node produced under ``key``."""
        ...

    def has(self, key: str) -> bool:
        """True when ``key`` is present (a cache hit will replay it)."""
        ...


class InMemoryCacheStore:
    """A process-local cache (tests, single-run dedupe). Not persistent."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Artifact, ...]] = {}

    def get(self, key: str) -> tuple[Artifact, ...] | None:
        return self._store.get(key)

    def put(self, key: str, artifacts: Sequence[Artifact]) -> None:
        self._store[key] = tuple(artifacts)

    def has(self, key: str) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)

    def keys(self) -> list[str]:
        return list(self._store)


class NullCacheStore:
    """A cache that never hits — every node always runs (cache disabled)."""

    def get(self, key: str) -> tuple[Artifact, ...] | None:
        return None

    def put(self, key: str, artifacts: Sequence[Artifact]) -> None:
        return None

    def has(self, key: str) -> bool:
        return False


class FileSystemCacheStore:
    """A durable on-disk cache: one JSON manifest per key under ``root``.

    The manifest records each artifact's logical ref + the on-disk path + content
    hash + size + metadata. A hit is only honoured when every recorded path still
    exists (a pruned working directory invalidates the entry), so the cache never
    replays a vanished artifact.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _manifest_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> tuple[Artifact, ...] | None:
        manifest = self._manifest_path(key)
        if not manifest.exists():
            return None
        try:
            payload = json.loads(manifest.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        artifacts: list[Artifact] = []
        for entry in payload.get("artifacts", []):
            path = Path(entry["path"])
            if not path.exists():
                return None  # an artifact was pruned → treat the whole entry as a miss
            artifacts.append(
                Artifact(
                    ref=ArtifactRef(
                        name=entry["name"],
                        kind=entry["kind"],
                        ext=entry["ext"],
                    ),
                    path=path,
                    sha256=entry.get("sha256", ""),
                    size_bytes=int(entry.get("size_bytes", 0)),
                    meta=entry.get("meta", {}),
                )
            )
        return tuple(artifacts)

    def put(self, key: str, artifacts: Sequence[Artifact]) -> None:
        payload = {
            "key": key,
            "artifacts": [
                {
                    "name": art.ref.name,
                    "kind": art.ref.kind.value,
                    "ext": art.ref.ext,
                    "path": str(art.path),
                    "sha256": art.sha256,
                    "size_bytes": art.size_bytes,
                    "meta": art.meta,
                }
                for art in artifacts
            ],
        }
        self._manifest_path(key).write_text(json.dumps(payload, indent=2), "utf-8")

    def has(self, key: str) -> bool:
        return self.get(key) is not None


__all__ = [
    "CacheStore",
    "FileSystemCacheStore",
    "InMemoryCacheStore",
    "NullCacheStore",
    "node_cache_key",
]
