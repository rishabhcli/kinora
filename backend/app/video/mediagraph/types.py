"""Local, self-owned domain types for the universal media transform graph.

A finished Kinora clip needs a fixed set of *derived* media — a normalised
master, a thumbnail, a poster, a preview GIF, a scrubbing sprite-sheet, burned-in
captions — regardless of which model (Wan, MiniMax, the Ken-Burns degradation
lane) produced the source clip. This package models that derivation as a
declarative DAG of media transforms.

This module owns **every** type the subsystem needs. The FINAL-round mandate is
that the round-1/round-2 ``normalize`` / ``delivery`` packages are *not* merged
and *cannot* be imported, so nothing here reaches outside ``app.video.mediagraph``
(the one exception is :func:`content_hash`, a tiny pure helper). The types are
pydantic v2 models / frozen dataclasses with no I/O, so the planning layer is
fully unit-testable without ffmpeg, a provider, a DB, or the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Media kinds & geometry
# --------------------------------------------------------------------------- #


class MediaKind(StrEnum):
    """The kind of a media artifact flowing through the graph.

    A node declares the kinds it consumes and produces; the graph validator uses
    these to type-check edges (a thumbnail node must be fed a *video* or *image*,
    never a caption file) before any ffmpeg runs.
    """

    #: An encoded video file (mp4/mov/webm) — the source clip or a derived master.
    VIDEO = "video"
    #: A still image (png/jpg/webp) — a thumbnail, poster, last-frame, sprite.
    IMAGE = "image"
    #: An audio file (wav/m4a) — an extracted or loudness-normalised track.
    AUDIO = "audio"
    #: An animated GIF preview.
    GIF = "gif"
    #: A timed-text captions file (vtt/srt) — an *input* to caption burn-in.
    CAPTIONS = "captions"
    #: A small JSON/text sidecar (e.g. a sprite-sheet's tile geometry manifest).
    SIDECAR = "sidecar"


#: A node-kind tag is a coarse classification used purely for introspection and
#: deterministic plan-explanations; it never affects scheduling.
class NodeKind(StrEnum):
    """A coarse category for a transform node (introspection only)."""

    PROBE = "probe"
    NORMALIZE = "normalize"
    EXTRACT_FRAME = "extract_frame"
    THUMBNAIL = "thumbnail"
    POSTER = "poster"
    PREVIEW_GIF = "preview_gif"
    SPRITE_SHEET = "sprite_sheet"
    CAPTION_BURN_IN = "caption_burn_in"
    LOUDNESS = "loudness"
    WATERMARK = "watermark"
    SOURCE = "source"


class Geometry(BaseModel):
    """An immutable output geometry (pixels).

    Kinora's film frame is vertical 720×1280, but the graph is provider-agnostic
    and works on any clip, so geometry is always carried explicitly rather than
    assumed.
    """

    model_config = ConfigDict(frozen=True)

    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def scale_expr(self) -> str:
        """The ffmpeg ``scale`` target ``WxH`` for this geometry."""
        return f"{self.width}x{self.height}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.scale_expr()


#: Kinora's canonical vertical film frame (mirrors degrade.FILM_SIZE; redefined
#: locally so this package imports nothing from app.render).
FILM_GEOMETRY = Geometry(width=720, height=1280)


# --------------------------------------------------------------------------- #
# Artifacts — the nodes' inputs / outputs
# --------------------------------------------------------------------------- #


class ArtifactRef(BaseModel):
    """A *reference* to a media artifact by logical name and kind.

    Nodes declare their inputs and outputs as refs (pure planning data). At
    execution time the engine resolves each ref to a concrete file path inside a
    per-run working directory. A ref carries no bytes and no path, so the whole
    plan graph is serialisable and content-hashable.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable logical name, unique within a graph (e.g. ``"master"``,
    #: ``"thumb"``, ``"sprite.png"``). Used to wire edges between nodes.
    name: str = Field(min_length=1, max_length=200)
    kind: MediaKind
    #: The file extension the artifact will be written with (no leading dot),
    #: e.g. ``"mp4"`` / ``"png"`` / ``"gif"`` / ``"vtt"``. Drives the ffmpeg muxer
    #: choice and the on-disk filename.
    ext: str = Field(min_length=1, max_length=8)

    @field_validator("ext")
    @classmethod
    def _strip_dot(cls, value: str) -> str:
        return value.lstrip(".").lower()

    @property
    def filename(self) -> str:
        """The deterministic on-disk filename for this artifact."""
        return f"{self.name}.{self.ext}"


class Artifact(BaseModel):
    """A concrete, materialised media artifact (a resolved :class:`ArtifactRef`).

    Produced by the engine once a node has run (or a cache hit is replayed). Holds
    the on-disk path, the content hash of the bytes, and a small free-form
    metadata bag (e.g. probed duration, sprite tile count). Never holds the bytes
    themselves — large media stays on disk.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ref: ArtifactRef
    path: Path
    #: SHA-256 of the produced bytes (``""`` until materialised, e.g. for a
    #: side-effect-free probe whose only output is metadata).
    sha256: str = ""
    size_bytes: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.ref.name

    @property
    def kind(self) -> MediaKind:
        return self.ref.kind


# --------------------------------------------------------------------------- #
# ffmpeg invocation plan (pure data — no subprocess here)
# --------------------------------------------------------------------------- #


class FfmpegInvocation(BaseModel):
    """One fully-resolved ffmpeg/ffprobe command line as pure data.

    The plan layer emits an ordered list of these; the executor hands each to an
    injectable runner. The argument list is *complete and deterministic* — given
    the same inputs it is byte-for-byte identical — which is what makes the plan
    unit-testable without ever spawning a process. ``binary`` is a logical name
    (``"ffmpeg"`` / ``"ffprobe"``) the runner resolves to a real path.
    """

    model_config = ConfigDict(frozen=True)

    #: Logical binary name — the runner maps it to a resolved executable.
    binary: str = "ffmpeg"
    #: The argument vector *after* the binary (never includes the binary itself).
    args: tuple[str, ...]
    #: The artifact this invocation produces (``None`` for a pure probe).
    produces: ArtifactRef | None = None
    #: A human label used in logs / plan explanations.
    label: str = ""
    #: When set, the invocation's stdout is the meaningful result (probes), not a
    #: file written to disk.
    captures_stdout: bool = False

    @field_validator("args", mode="before")
    @classmethod
    def _coerce_args(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(str(a) for a in value)
        raise TypeError("args must be a sequence of strings")

    def command(self) -> tuple[str, ...]:
        """The full logical command vector including the binary name."""
        return (self.binary, *self.args)


class RunResult(BaseModel):
    """The outcome of executing a single :class:`FfmpegInvocation`.

    Returned by the injectable runner. ``stdout`` carries probe JSON when
    :attr:`FfmpegInvocation.captures_stdout`. The engine records ``ok`` / error
    per invocation so a failed branch can be isolated without sinking siblings.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    #: Wall time, seconds (best-effort; 0.0 from fakes).
    duration_s: float = 0.0


# --------------------------------------------------------------------------- #
# Node / engine status & results
# --------------------------------------------------------------------------- #


class NodeStatus(StrEnum):
    """A node's terminal-or-transient state in the engine's lifecycle.

    Distinct from :class:`app.render.dag.NodeState`; this graph schedules *media
    transforms*, not shots.
    """

    PENDING = "pending"  # waiting on upstream nodes
    READY = "ready"  # all inputs produced; eligible to run
    RUNNING = "running"  # dispatched to the runner
    SUCCEEDED = "succeeded"  # produced its outputs
    CACHED = "cached"  # skipped — outputs already exist for this content hash
    FAILED = "failed"  # its own invocation(s) failed
    SKIPPED = "skipped"  # an upstream input failed (failure isolation)

    @property
    def is_terminal(self) -> bool:
        return self in (
            NodeStatus.SUCCEEDED,
            NodeStatus.CACHED,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
        )

    @property
    def is_ok(self) -> bool:
        """Did this node make its outputs available (run or replayed from cache)?"""
        return self in (NodeStatus.SUCCEEDED, NodeStatus.CACHED)


class NodeResult(BaseModel):
    """The per-node outcome of an engine run."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    status: NodeStatus
    #: Artifacts the node made available (empty when failed/skipped).
    artifacts: tuple[Artifact, ...] = ()
    #: When failed, the upstream cause / ffmpeg error tail.
    error: str | None = None
    #: The content hash this node ran under (its cache key).
    content_hash: str = ""
    #: How many ffmpeg invocations the node issued (0 when cached/skipped).
    invocations: int = 0

    def artifact(self, name: str) -> Artifact | None:
        """The produced artifact with logical ``name``, or ``None``."""
        return next((a for a in self.artifacts if a.name == name), None)


class GraphResult(BaseModel):
    """The outcome of executing a whole media graph.

    Captures the realised batch waves (parallel fan-out), every node's result,
    and an index of all produced artifacts by logical name. ``partial`` is True
    when some nodes failed but others still produced usable derivatives — the
    explicit *partial-result* contract: a broken sprite-sheet must not deny the
    reader a thumbnail.
    """

    model_config = ConfigDict(frozen=True)

    results: dict[str, NodeResult]
    batches: tuple[tuple[str, ...], ...] = ()

    @property
    def succeeded(self) -> list[str]:
        return [nid for nid, r in self.results.items() if r.status is NodeStatus.SUCCEEDED]

    @property
    def cached(self) -> list[str]:
        return [nid for nid, r in self.results.items() if r.status is NodeStatus.CACHED]

    @property
    def failed(self) -> list[str]:
        return [nid for nid, r in self.results.items() if r.status is NodeStatus.FAILED]

    @property
    def skipped(self) -> list[str]:
        return [nid for nid, r in self.results.items() if r.status is NodeStatus.SKIPPED]

    @property
    def ok(self) -> bool:
        """True when *every* node made its outputs available (none failed/skipped)."""
        return all(r.status.is_ok for r in self.results.values())

    @property
    def partial(self) -> bool:
        """True when some nodes produced outputs but at least one did not."""
        ok_any = any(r.status.is_ok for r in self.results.values())
        bad_any = any(not r.status.is_ok for r in self.results.values())
        return ok_any and bad_any

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    @property
    def max_parallelism(self) -> int:
        """The widest wave released — the realised transform fan-out."""
        return max((len(b) for b in self.batches), default=0)

    def artifacts(self) -> dict[str, Artifact]:
        """Every produced artifact, indexed by its logical name (last write wins)."""
        index: dict[str, Artifact] = {}
        for result in self.results.values():
            for art in result.artifacts:
                index[art.name] = art
        return index

    def artifact(self, name: str) -> Artifact | None:
        return self.artifacts().get(name)

    def require_artifact(self, name: str) -> Artifact:
        """The produced artifact named ``name``; raises :class:`KeyError` if absent.

        Use when a caller knows a derivative must exist (e.g. the master after a
        successful run); :meth:`artifact` is the optional-returning variant.
        """
        art = self.artifact(name)
        if art is None:
            raise KeyError(f"no produced artifact named {name!r}")
        return art


# --------------------------------------------------------------------------- #
# Content hashing — the cache key primitive
# --------------------------------------------------------------------------- #


def content_hash(*parts: Any) -> str:
    """A stable SHA-256 over an ordered sequence of hashable descriptors.

    The cache key for a node is ``content_hash(source_hash, node_signature,
    *upstream_hashes)``. Deterministic and order-sensitive: the same inputs always
    yield the same digest, so an already-produced derivative is skipped on re-run.
    Mappings are canonicalised (sorted keys) so dict ordering never perturbs it.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(b"\x1e")  # record separator, so ("a","b") != ("ab",)
        hasher.update(_canonical_bytes(part))
    return hasher.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    """Canonical byte encoding for a hashable descriptor (order-stable).

    Every encoding is prefixed with a one-byte type tag so distinct types never
    collide (``1`` the int must not hash equal to ``"1"`` the str, nor ``True`` to
    ``1``). bool is checked before int because ``bool`` is an ``int`` subclass.
    """
    if isinstance(value, bytes):
        return b"y" + value
    if isinstance(value, str):
        return b"s" + value.encode("utf-8")
    if isinstance(value, bool):
        return b"b\x01" if value else b"b\x00"
    if isinstance(value, int):
        return b"i" + repr(value).encode("utf-8")
    if isinstance(value, float):
        return b"f" + repr(value).encode("utf-8")
    if value is None:
        return b"n"
    if isinstance(value, Mapping):
        items = sorted((str(k), value[k]) for k in value)
        body = b",".join(_canonical_bytes(k) + b":" + _canonical_bytes(v) for k, v in items)
        return b"m{" + body + b"}"
    if isinstance(value, Sequence):
        return b"q[" + b",".join(_canonical_bytes(v) for v in value) + b"]"
    return b"r" + repr(value).encode("utf-8")


def hash_bytes(data: bytes) -> str:
    """SHA-256 of raw bytes (the source-clip content key)."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file streamed in chunks (large media never fully buffered)."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            hasher.update(block)
    return hasher.hexdigest()


__all__ = [
    "FILM_GEOMETRY",
    "Artifact",
    "ArtifactRef",
    "FfmpegInvocation",
    "Geometry",
    "GraphResult",
    "MediaKind",
    "NodeKind",
    "NodeResult",
    "NodeStatus",
    "RunResult",
    "content_hash",
    "hash_bytes",
    "hash_file",
]
