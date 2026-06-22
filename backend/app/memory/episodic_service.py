"""Episodic / vector store service — "what worked before" (kinora.md §8.2).

This is the memory that makes generation *increasingly accurate across sessions*:
every accepted shot is embedded (from its last frame / keyframe, or its described
visuals as a fallback) and stored, so a later, similar beat can retrieve the
prior shots that passed QA and condition on them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db.models.enums import ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.shot import ShotRepo
from app.memory.interfaces import BlobStore, Embedder, EpisodicShotRef


class EpisodicService:
    """Search nearest accepted shots and log new ones with their embedding."""

    def __init__(
        self,
        *,
        shots: ShotRepo,
        embedder: Embedder,
        blob_store: BlobStore | None = None,
        url_ttl: int = 3600,
    ) -> None:
        self._shots = shots
        self._embedder = embedder
        self._store = blob_store
        self._ttl = url_ttl

    async def search(
        self,
        book_id: str,
        *,
        query_embedding: list[float] | None = None,
        query_image_bytes: bytes | None = None,
        described_visuals_text: str | None = None,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[EpisodicShotRef]:
        """Return the ``k`` nearest accepted shots, embedding the query if needed."""
        embedding = await self._resolve_query_embedding(
            query_embedding, query_image_bytes, described_visuals_text
        )
        if embedding is None:
            return []
        rows = await self._shots.episodic_search(book_id, embedding, k=k, filters=filters)
        return [self._to_ref(row) for row in rows]

    async def log(
        self,
        *,
        book_id: str,
        status: ShotStatus = ShotStatus.ACCEPTED,
        shot_id: str | None = None,
        beat_id: str | None = None,
        scene_id: str | None = None,
        source_span: dict[str, Any] | None = None,
        render_mode: str | None = None,
        prompt: str | None = None,
        negative_prompt: str | None = None,
        seed: int | None = None,
        reference_set_hash: str | None = None,
        reference_image_ids: list[str] | None = None,
        duration_s: float | None = None,
        output: dict[str, Any] | None = None,
        narration: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
        canon_version_at_render: int | None = None,
        shot_hash: str | None = None,
        last_frame_bytes: bytes | None = None,
        keyframe_bytes: bytes | None = None,
        described_visuals_text: str | None = None,
    ) -> Shot:
        """Persist a shot + its QA, computing and storing its retrieval embedding."""
        embedding = await self._embed_for_log(
            last_frame_bytes, keyframe_bytes, described_visuals_text
        )

        fields: dict[str, Any] = {
            "book_id": book_id,
            "status": status,
            "beat_id": beat_id,
            "scene_id": scene_id,
            "source_span": source_span,
            "render_mode": render_mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seed": seed,
            "reference_set_hash": reference_set_hash,
            "reference_image_ids": reference_image_ids,
            "duration_s": duration_s,
            "output": output,
            "narration": narration,
            "qa": qa,
            "cost": cost,
            "canon_version_at_render": canon_version_at_render,
            "shot_hash": shot_hash,
        }
        if embedding is not None:
            fields["embedding"] = embedding
        if status is ShotStatus.ACCEPTED:
            fields["accepted_at"] = datetime.now(UTC)
        fields = {key: value for key, value in fields.items() if value is not None}

        if shot_id is not None and await self._shots.get(shot_id) is not None:
            updated = await self._shots.update(shot_id, **fields)
            assert updated is not None  # noqa: S101 - existence just checked
            return updated
        if shot_id is not None:
            fields["id"] = shot_id
        return await self._shots.create(**fields)

    async def _resolve_query_embedding(
        self,
        query_embedding: list[float] | None,
        query_image_bytes: bytes | None,
        described_visuals_text: str | None,
    ) -> list[float] | None:
        if query_embedding is not None:
            return query_embedding
        if query_image_bytes is not None:
            vectors = await self._embedder.embed_images([query_image_bytes])
            return vectors[0] if vectors else None
        if described_visuals_text:
            vectors = await self._embedder.embed_texts([described_visuals_text])
            return vectors[0] if vectors else None
        return None

    async def _embed_for_log(
        self,
        last_frame_bytes: bytes | None,
        keyframe_bytes: bytes | None,
        described_visuals_text: str | None,
    ) -> list[float] | None:
        image = last_frame_bytes or keyframe_bytes
        if image is not None:
            vectors = await self._embedder.embed_images([image])
            return vectors[0] if vectors else None
        if described_visuals_text:
            vectors = await self._embedder.embed_texts([described_visuals_text])
            return vectors[0] if vectors else None
        return None

    def _to_ref(self, shot: Shot) -> EpisodicShotRef:
        output = shot.output or {}
        return EpisodicShotRef(
            shot_id=shot.id,
            beat_id=shot.beat_id,
            scene_id=shot.scene_id,
            render_mode=shot.render_mode,
            seed=shot.seed,
            reference_image_ids=list(shot.reference_image_ids or []),
            qa=shot.qa,
            clip_url=self._presign(output.get("clip_key")),
            last_frame_url=self._presign(output.get("last_frame_key")),
        )

    def _presign(self, key: str | None) -> str | None:
        if key is None or self._store is None:
            return None
        # generate_presigned_url signs locally (no network round-trip).
        return self._store.presigned_get_url(key, ttl=self._ttl)


__all__ = ["EpisodicService"]
