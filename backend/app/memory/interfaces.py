"""Typed seams of the memory layer.

This module is the layer's *contract surface*:

* the :class:`CanonSlice` family — the JSON-serializable result of
  ``canon.query`` (kinora.md §8.4) that the agents (§7) consume;
* :class:`ShotSpec` — the Cinematographer's shot specification (§7.1) that flows
  into ``shot.render``;
* the structural :class:`Embedder` / :class:`BlobStore` protocols the services
  depend on (so the real provider/object-store *and* test doubles fit without
  inheritance);
* the :class:`RenderEnqueuer` / :class:`ShotPlanner` protocols owned by **later
  phases** — Phase 4 never implements the render queue or the Adapter, it only
  declares the seam and ships a :class:`NotWired` default.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.db.models.enums import RenderPriority

# --------------------------------------------------------------------------- #
# canon.query result (the slice the agents consume, §8.4)
# --------------------------------------------------------------------------- #


class RefImage(BaseModel):
    """A locked reference image: its object key plus a (presigned) fetch URL."""

    key: str
    url: str | None = None
    pose: str | None = None
    locked: bool = False


class CanonEntitySlice(BaseModel):
    """One canon entity resolved *as of* a beat, with presigned reference URLs."""

    entity_key: str
    type: str
    name: str
    version: int
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    appearance: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    voice_ref_url: str | None = None
    style_tokens: dict[str, Any] | None = None
    reference_images: list[RefImage] = Field(default_factory=list)
    valid_from_beat: int
    valid_to_beat: int | None = None


class StateSlice(BaseModel):
    """An *active* continuity fact at the beat (retired facts never appear)."""

    state_id: str
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    valid_to_beat: int | None = None


class EndpointFrame(BaseModel):
    """The previous accepted shot's last frame — the continuation anchor (§9.3)."""

    shot_id: str
    last_frame_key: str | None = None
    last_frame_url: str | None = None


class EpisodicShotRef(BaseModel):
    """A nearest prior accepted shot — "what worked before" (§8.2)."""

    shot_id: str
    beat_id: str | None = None
    scene_id: str | None = None
    render_mode: str | None = None
    seed: int | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    qa: dict[str, Any] | None = None
    clip_url: str | None = None
    last_frame_url: str | None = None


class CanonSlice(BaseModel):
    """The result of ``canon.query`` — *only* what this beat needs (§8.4).

    Never the whole book: characters present (+ props) resolved at this beat's
    version, the active location, the scene's style tokens, the active continuity
    facts, the previous endpoint frame, and the top-k similar prior shots.
    """

    book_id: str
    beat_id: str
    beat_index: int
    scene_id: str | None = None
    characters: list[CanonEntitySlice] = Field(default_factory=list)
    location: CanonEntitySlice | None = None
    props: list[CanonEntitySlice] = Field(default_factory=list)
    style: CanonEntitySlice | None = None
    active_states: list[StateSlice] = Field(default_factory=list)
    previous_endpoint: EndpointFrame | None = None
    episodic: list[EpisodicShotRef] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shot specification (Cinematographer → shot.render, §7.1)
# --------------------------------------------------------------------------- #


class ShotSpec(BaseModel):
    """A fully-resolved shot specification (the Cinematographer's output, §7.1)."""

    book_id: str
    beat_id: str
    scene_id: str | None = None
    shot_id: str | None = None
    render_mode: str = "reference_to_video"
    prompt: str = ""
    negative_prompt: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)
    camera: dict[str, Any] | None = None
    seed: int = 0
    target_duration_s: float = 5.0
    canon_version_at_render: int = 1
    reference_set_hash: str | None = None
    shot_hash: str | None = None
    end_frame_ref: str | None = None


# --------------------------------------------------------------------------- #
# Structural dependencies (satisfied by the real provider / object store)
# --------------------------------------------------------------------------- #


@runtime_checkable
class Embedder(Protocol):
    """The slice of the embeddings provider the memory layer needs (1152-d)."""

    async def embed_images(self, images: list[bytes]) -> list[list[float]]: ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class BlobStore(Protocol):
    """The slice of the object store the memory layer needs (S3/MinIO/OSS)."""

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str: ...


# --------------------------------------------------------------------------- #
# Seams owned by later phases (NOT implemented in Phase 4)
# --------------------------------------------------------------------------- #


class NotWired(RuntimeError):  # noqa: N818 - public name in task contract
    """Raised by a DI seam that a later phase is responsible for wiring."""


class RenderEnqueuer(Protocol):
    """Enqueue a shot for rendering — the Scheduler/queue, injected by Phase 8."""

    async def enqueue(
        self,
        shot_spec: ShotSpec,
        priority: RenderPriority,
        cancel_token: str | None = None,
    ) -> str:
        """Enqueue ``shot_spec`` and return the render ``job_id``."""
        ...


class ShotPlanner(Protocol):
    """Decompose a scene into shots — the Adapter, injected by a later phase."""

    async def plan_scene(self, scene_id: str) -> list[ShotSpec]:
        """Return the ordered shot list for ``scene_id``."""
        ...


class NotWiredRenderEnqueuer:
    """Default :class:`RenderEnqueuer` — raises until Phase 8 injects the real one."""

    async def enqueue(
        self,
        shot_spec: ShotSpec,
        priority: RenderPriority,
        cancel_token: str | None = None,
    ) -> str:
        raise NotWired("render backend injected by Phase 8")


class NotWiredShotPlanner:
    """Default :class:`ShotPlanner` — raises until the Adapter phase injects it."""

    async def plan_scene(self, scene_id: str) -> list[ShotSpec]:
        raise NotWired("shot planner injected by Phase 8")


__all__ = [
    "BlobStore",
    "CanonEntitySlice",
    "CanonSlice",
    "Embedder",
    "EndpointFrame",
    "EpisodicShotRef",
    "NotWired",
    "NotWiredRenderEnqueuer",
    "NotWiredShotPlanner",
    "RefImage",
    "RenderEnqueuer",
    "ShotPlanner",
    "ShotSpec",
    "StateSlice",
]
