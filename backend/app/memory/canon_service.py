"""Canon graph service — the retrieval policy, time-travel reads, and forgetting.

This is the heart of the Track-1 memory layer:

* :meth:`query` implements ``canon.query`` (§8.4) **exactly** — for one beat it
  returns *only* the characters present (resolved at this beat's version), the
  active location, the scene's style tokens, the active continuity facts, the
  previous accepted endpoint frame, and the top-k similar prior shots. Never the
  whole book.
* :meth:`get_entity` is a time-travel read (``canon.get_entity``, §8.3).
* :meth:`upsert_entity` is the Continuity Supervisor's write; it computes and
  stores the appearance embedding from the locked reference image (§8.1, §9.5).
* :meth:`assert_state` / :meth:`retire_state` implement forgetting by scoping a
  fact to the beat interval over which it is true (§8.5): a retired fact drops
  out of active retrieval but survives for backward/time-travel reads.
"""

from __future__ import annotations

from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.beat import Beat
from app.db.models.entity import Entity
from app.db.models.enums import EntityType, ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.beat import BeatRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotRepo
from app.memory.episodic_service import EpisodicService
from app.memory.interfaces import (
    BlobStore,
    CanonEntitySlice,
    CanonSlice,
    Embedder,
    EndpointFrame,
    RefImage,
    StateSlice,
)

logger = get_logger("app.memory.canon")

# Resolving "as of the latest version" = resolving at a beat beyond any real one,
# so ``get_as_of_beat`` returns the still-open (current) version.
_LATEST_BEAT = 2**31 - 1


class UnknownBeatError(LookupError):
    """Raised when ``canon.query`` is asked about a beat that does not exist."""


class CanonService:
    """Read the relevant canon slice; write versioned entities and facts."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: Embedder,
        blob_store: BlobStore | None = None,
        episodic: EpisodicService | None = None,
        url_ttl: int = 3600,
    ) -> None:
        self.session = session
        self._entities = EntityRepo(session)
        self._states = ContinuityStateRepo(session)
        self._scenes = SceneRepo(session)
        self._beats = BeatRepo(session)
        self._shots = ShotRepo(session)
        self._embedder = embedder
        self._store = blob_store
        self._ttl = url_ttl
        self._episodic = episodic or EpisodicService(
            shots=self._shots, embedder=embedder, blob_store=blob_store, url_ttl=url_ttl
        )

    # --- canon.query (§8.4) ------------------------------------------------- #

    async def query(
        self,
        book_id: str,
        beat_id: str,
        kinds: list[EntityType] | list[str] | None = None,
        *,
        episodic_k: int = 3,
    ) -> CanonSlice:
        """Return only the canon this beat needs (§8.4); never the whole book."""
        beat = await self._beats.get(beat_id)
        if beat is None or beat.book_id != book_id:
            raise UnknownBeatError(f"unknown beat_id for book: {beat_id}")

        ordinal = beat.beat_index
        want = self._kind_filter(kinds)

        present = await self._resolve_present(book_id, beat.entities or [], ordinal)
        characters = self._slices(present, EntityType.CHARACTER, want)
        props = self._slices(present, EntityType.PROP, want)
        location = await self._active_location(book_id, present, ordinal, want)
        style = await self._scene_style(book_id, beat.scene_id, ordinal, want)

        states = await self._states.active_states_at_beat(book_id, ordinal)
        previous = await self._previous_endpoint(book_id, ordinal)

        episodic = (
            await self._episodic.search(
                book_id, described_visuals_text=beat.described_visuals, k=episodic_k
            )
            if beat.described_visuals
            else []
        )

        return CanonSlice(
            book_id=book_id,
            beat_id=beat_id,
            beat_index=ordinal,
            scene_id=beat.scene_id,
            characters=characters,
            location=location,
            props=props,
            style=style,
            active_states=[self._state_slice(s) for s in states],
            previous_endpoint=previous,
            episodic=episodic,
        )

    # --- canon.get_entity (time-travel read, §8.3) -------------------------- #

    async def get_entity(
        self, book_id: str, entity_key: str, at_beat: int | None = None
    ) -> CanonEntitySlice | None:
        """Resolve a versioned entity *as of* ``at_beat`` (latest when omitted)."""
        beat = _LATEST_BEAT if at_beat is None else at_beat
        entity = await self._entities.get_as_of_beat(book_id, entity_key, beat)
        return self._entity_slice(entity) if entity is not None else None

    # --- canon.upsert_entity (Continuity Supervisor write, §8.1) ------------ #

    async def upsert_entity(
        self,
        *,
        book_id: str,
        entity_key: str,
        entity_type: EntityType,
        name: str,
        valid_from_beat: int,
        aliases: list[str] | None = None,
        description: str | None = None,
        appearance: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
        style_tokens: dict[str, Any] | None = None,
        first_appearance: dict[str, Any] | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Write a new entity version; embed the locked reference image if present."""
        embedding = await self._appearance_embedding(appearance)
        return await self._entities.upsert_new_version(
            book_id=book_id,
            entity_key=entity_key,
            entity_type=entity_type,
            name=name,
            valid_from_beat=valid_from_beat,
            aliases=aliases,
            description=description,
            appearance=appearance,
            voice=voice,
            style_tokens=style_tokens,
            first_appearance=first_appearance,
            embedding=embedding,
            entity_id=entity_id,
        )

    # --- canon.assert_state / retire_state — forgetting (§8.5) -------------- #

    async def assert_state(
        self,
        *,
        book_id: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        source_span: dict[str, Any] | None = None,
        state_id: str | None = None,
    ) -> str:
        """Add a versioned fact valid from ``valid_from_beat`` (open-ended)."""
        return await self._states.assert_state(
            book_id=book_id,
            subject_entity_key=subject_entity_key,
            predicate=predicate,
            object_value=object_value,
            valid_from_beat=valid_from_beat,
            source_span=source_span,
            state_id=state_id,
        )

    async def retire_state(self, state_id: str, valid_to_beat: int) -> None:
        """Forgetting: close a fact's validity interval (§8.5)."""
        await self._states.retire_state(state_id, valid_to_beat)

    async def active_states_at_beat(
        self, book_id: str, beat: int, *, subject_entity_key: str | None = None
    ) -> list[StateSlice]:
        """Return only the facts whose interval contains ``beat`` (retired excluded)."""
        states = await self._states.active_states_at_beat(
            book_id, beat, subject_entity_key=subject_entity_key
        )
        return [self._state_slice(s) for s in states]

    # --- internals ---------------------------------------------------------- #

    async def _resolve_present(
        self, book_id: str, entity_keys: list[str], ordinal: int
    ) -> list[Entity]:
        # One batched query instead of an N+1 over the beat's entities (§8.4).
        present = await self._entities.get_present_as_of_beat(book_id, entity_keys, ordinal)
        # Preserve the input order of ``entity_keys`` (skip those with no active version).
        return [present[key] for key in entity_keys if key in present]

    def _slices(
        self,
        present: list[Entity],
        kind: EntityType,
        want: set[EntityType] | None,
    ) -> list[CanonEntitySlice]:
        if want is not None and kind not in want:
            return []
        return [self._entity_slice(e) for e in present if e.type == kind]

    async def _active_location(
        self,
        book_id: str,
        present: list[Entity],
        ordinal: int,
        want: set[EntityType] | None,
    ) -> CanonEntitySlice | None:
        if want is not None and EntityType.LOCATION not in want:
            return None
        in_beat = [e for e in present if e.type == EntityType.LOCATION]
        location = in_beat[0] if in_beat else None
        if location is None:
            actives = await self._entities.list_active_at_beat(
                book_id, ordinal, kinds=[EntityType.LOCATION]
            )
            location = actives[0] if actives else None
        return self._entity_slice(location) if location is not None else None

    async def _scene_style(
        self,
        book_id: str,
        scene_id: str | None,
        ordinal: int,
        want: set[EntityType] | None,
    ) -> CanonEntitySlice | None:
        if want is not None and EntityType.STYLE not in want:
            return None
        style: Entity | None = None
        if scene_id is not None:
            style_key = await self._scenes.style_for_scene(scene_id)
            if style_key is not None:
                style = await self._entities.get_as_of_beat(book_id, style_key, ordinal)
        if style is None:
            # Fall back to the book's default (only) active style node.
            actives = await self._entities.list_active_at_beat(
                book_id, ordinal, kinds=[EntityType.STYLE]
            )
            style = actives[0] if actives else None
        return self._entity_slice(style) if style is not None else None

    async def _previous_endpoint(self, book_id: str, ordinal: int) -> EndpointFrame | None:
        # The most recent accepted shot whose beat precedes this one — its last
        # frame is the continuation anchor (§9.3). Read-only projection over the
        # existing shots/beats tables (no new persistence).
        stmt = (
            select(Shot)
            .join(Beat, Beat.id == Shot.beat_id)
            .where(
                Shot.book_id == book_id,
                Shot.status == ShotStatus.ACCEPTED,
                Beat.beat_index < ordinal,
            )
            .order_by(Beat.beat_index.desc())
            .limit(1)
        )
        shot = (await self.session.execute(stmt)).scalars().first()
        if shot is None:
            return None
        last_key = (shot.output or {}).get("last_frame_key")
        return EndpointFrame(
            shot_id=shot.id,
            last_frame_key=last_key,
            last_frame_url=self._presign(last_key),
        )

    async def _appearance_embedding(
        self, appearance: dict[str, Any] | None
    ) -> list[float] | None:
        if not appearance or self._store is None:
            return None
        key = self._locked_ref_key(appearance)
        if key is None:
            return None
        # Fetch the locked reference image off the event loop; embed it only if
        # it is actually present in object storage.
        if not await anyio.to_thread.run_sync(self._store.exists, key):
            # A locked reference key is declared but its image is missing — the
            # Critic's identity (CCS) check will be skipped (§9.5). Surface it so a
            # missing upload isn't silently degrading QA.
            logger.warning("canon.locked_reference_missing", key=key)
            return None
        data = await anyio.to_thread.run_sync(self._store.get_bytes, key)
        vectors = await self._embedder.embed_images([data])
        return vectors[0] if vectors else None

    def _entity_slice(self, entity: Entity) -> CanonEntitySlice:
        voice = entity.voice or None
        voice_ref_url: str | None = None
        if voice:
            audio_key = voice.get("reference_audio_key")
            if isinstance(audio_key, str):
                voice_ref_url = self._presign(audio_key)
        return CanonEntitySlice(
            entity_key=entity.entity_key,
            type=entity.type.value,
            name=entity.name,
            version=entity.version,
            description=entity.description,
            aliases=list(entity.aliases or []),
            appearance=entity.appearance,
            voice=voice,
            voice_ref_url=voice_ref_url,
            style_tokens=entity.style_tokens,
            reference_images=self._reference_images(entity.appearance or {}),
            valid_from_beat=entity.valid_from_beat,
            valid_to_beat=entity.valid_to_beat,
        )

    def _reference_images(self, appearance: dict[str, Any]) -> list[RefImage]:
        images: list[RefImage] = []
        raw = appearance.get("reference_images")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                key = item.get("key") or item.get("oss_key")
                if isinstance(key, str):
                    images.append(
                        RefImage(
                            key=key,
                            url=self._presign(key),
                            pose=item.get("pose"),
                            locked=bool(item.get("locked", False)),
                        )
                    )
        keys = appearance.get("reference_image_keys")
        if isinstance(keys, list):
            locked = bool(appearance.get("locked", False))
            for key in keys:
                if isinstance(key, str):
                    images.append(RefImage(key=key, url=self._presign(key), locked=locked))
        return images

    @staticmethod
    def _locked_ref_key(appearance: dict[str, Any]) -> str | None:
        raw = appearance.get("reference_images")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("locked"):
                    key = item.get("key") or item.get("oss_key")
                    if isinstance(key, str):
                        return key
        keys = appearance.get("reference_image_keys")
        if isinstance(keys, list) and appearance.get("locked") and keys:
            first = keys[0]
            if isinstance(first, str):
                return first
        return None

    @staticmethod
    def _state_slice(state: Any) -> StateSlice:
        return StateSlice(
            state_id=state.id,
            subject_entity_key=state.subject_entity_key,
            predicate=state.predicate,
            object_value=state.object_value,
            valid_from_beat=state.valid_from_beat,
            valid_to_beat=state.valid_to_beat,
        )

    @staticmethod
    def _kind_filter(
        kinds: list[EntityType] | list[str] | None,
    ) -> set[EntityType] | None:
        if kinds is None:
            return None
        out: set[EntityType] = set()
        for kind in kinds:
            if isinstance(kind, EntityType):
                out.add(kind)
            else:
                try:
                    out.add(EntityType(kind))
                except ValueError:
                    continue
        return out or None

    def _presign(self, key: str | None) -> str | None:
        if key is None or self._store is None:
            return None
        return self._store.presigned_get_url(key, ttl=self._ttl)


__all__ = ["CanonService", "UnknownBeatError"]
