"""Identity lock — one-time canonical keyframes + per-character voices (§9.1 step 5).

For each **principal** character (one appearing in multiple beats) this:

* generates 1–2 canonical keyframe reference images with ``image.generate`` from
  the character's appearance description conditioned on the scene style tokens,
  uploads them to object storage under ``refs/<book>/<entity>/...``, and
  re-upserts the entity with those images marked **locked** — which makes
  :class:`app.memory.canon_service.CanonService` compute and store the appearance
  embedding from the locked reference (§8.1, §9.5);
* assigns a **distinct preset Qwen3-TTS voice** per principal plus a dedicated
  **narrator** voice, stored on the entity. These are real Model-Studio preset
  voices (the same family the TTS provider's ``synthesize(voice_id=...)`` accepts,
  e.g. ``"Cherry"``); we do **not** attempt to clone from nonexistent reference
  audio.

These locked refs + voices are produced once and reused by every later shot —
the appearance/voice are paid for once and amortised across the whole book.

Note: the providers layer exposes ``synthesize``/``clone_voice`` but no preset
voice catalogue, so the catalogue of real preset voice ids lives here (a plain
constant of Model-Studio voice names — no change to the providers is required).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models.beat import Beat
from app.db.models.enums import EntityType
from app.ingest.canon_build import DEFAULT_ART_DIRECTION, CanonEntity
from app.memory.canon_service import CanonService
from app.memory.interfaces import BlobStore
from app.providers import Providers
from app.storage.object_store import keys

logger = get_logger("app.ingest.identity_lock")

#: Concrete Model-Studio preset model the preset voices below belong to.
PRESET_TTS_MODEL = "qwen3-tts-flash"
_PNG_CONTENT_TYPE = "image/png"
_DEFAULT_KEYFRAME_SIZE = "928*1664"  # 9:16, in qwen-image-plus's allowed size set
_KEYFRAME_NEGATIVE = (
    "extra fingers, deformed hands, warped face, multiple people, crowd, text, "
    "watermark, logo, inconsistent design, blurry, low quality"
)
NARRATOR_ENTITY_KEY = "char_narrator"


@dataclass(frozen=True)
class PresetVoice:
    """A real Model-Studio Qwen3-TTS preset voice (id + a coarse timbre tag)."""

    voice_id: str
    gender: str


#: Real preset Qwen3-TTS voice ids (Model Studio), English-first ordering.
#: Restricted to voices verified available on the hosted ``qwen3-tts-flash``
#: snapshot we pin (see app.providers.tts._TTS_MODEL_SNAPSHOTS). The snapshot's
#: supported set is a subset of the alias's — Serena/Aiden/Chelsie/Vivian/Arthur
#: return 400 InvalidParameter, so they are omitted here (the TTS provider also
#: falls back to a known-good voice as defense in depth).
PRESET_VOICES: tuple[PresetVoice, ...] = (
    PresetVoice("Cherry", "female"),
    PresetVoice("Ryan", "male"),
    PresetVoice("Jennifer", "female"),
    PresetVoice("Eric", "male"),
    PresetVoice("Katerina", "female"),
    PresetVoice("Dylan", "male"),
    PresetVoice("Elias", "male"),
)
#: A warm, energetic voice reserved for narration (kept out of the principal pool).
NARRATOR_VOICE = PresetVoice("Ethan", "male")


class IdentityLockResult(BaseModel):
    """Outcome of identity lock (principals, their voices, and keyframe keys)."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    principals: list[str] = Field(default_factory=list)
    #: entity_key -> assigned preset voice_id (includes the narrator entity).
    voices: dict[str, str] = Field(default_factory=dict)
    narrator_voice: str = NARRATOR_VOICE.voice_id
    #: entity_key -> the object keys of its locked reference images.
    keyframe_keys: dict[str, list[str]] = Field(default_factory=dict)


def _seed_for(entity_key: str, pose_index: int) -> int:
    """Deterministic per-(entity, pose) image seed so keyframes are reproducible."""
    digest = hashlib.sha1(f"{entity_key}:{pose_index}".encode()).hexdigest()[:8]
    return int(digest, 16)


def _keyframe_prompt(entity: CanonEntity, style_tokens: dict[str, Any] | None) -> str:
    """Build the keyframe image prompt from appearance + scene style tokens."""
    tokens = style_tokens or {}
    art = str(tokens.get("art_direction") or DEFAULT_ART_DIRECTION)
    palette = str(tokens.get("palette") or "")
    lens = str(tokens.get("lens") or "")
    description = entity.description or entity.name
    parts = [
        f"{art}.",
        f"Character reference sheet of {entity.name}: {description}.",
        "Full figure, neutral plain background, single character, consistent design.",
    ]
    if palette:
        parts.append(f"Palette: {palette}.")
    if lens:
        parts.append(f"Lens: {lens}.")
    return " ".join(parts)


def assign_voices(principal_keys: Sequence[str]) -> dict[str, str]:
    """Assign a distinct preset voice to each principal (deterministic by key order)."""
    pool = [v for v in PRESET_VOICES if v.voice_id != NARRATOR_VOICE.voice_id]
    assignments: dict[str, str] = {}
    for index, key in enumerate(sorted(principal_keys)):
        assignments[key] = pool[index % len(pool)].voice_id
    return assignments


async def _beat_counts(canon: CanonService, book_id: str) -> dict[str, int]:
    """Count how many beats reference each canon entity_key."""
    rows = (
        (await canon.session.execute(select(Beat).where(Beat.book_id == book_id)))
        .scalars()
        .all()
    )
    counts: defaultdict[str, int] = defaultdict(int)
    for beat in rows:
        for key in beat.entities or []:
            counts[key] += 1
    return dict(counts)


async def lock_identities(
    *,
    book_id: str,
    canon: CanonService,
    characters: Sequence[CanonEntity],
    providers: Providers,
    blob_store: BlobStore,
    style_tokens: dict[str, Any] | None = None,
    poses: Sequence[str] = ("front",),
    min_beats: int = 2,
    keyframe_size: str = _DEFAULT_KEYFRAME_SIZE,
) -> IdentityLockResult:
    """Lock principal characters' appearance (keyframes) + assign preset voices.

    Args:
        book_id: the book being locked.
        canon: a :class:`CanonService` bound to the active unit-of-work (it
            recomputes the appearance embedding when the locked refs are upserted).
        characters: the deduplicated character entities (from canon build).
        providers: live providers (uses ``providers.image`` for keyframes).
        blob_store: object store the keyframe PNGs are uploaded to.
        style_tokens: the Style node's tokens (palette/lens/art) for the prompt.
        poses: which canonical poses to render per principal (1–2).
        min_beats: a character is a *principal* when it appears in ≥ this many beats.
        keyframe_size: image-gen size string.
    """
    counts = await _beat_counts(canon, book_id)
    principals = [c for c in characters if counts.get(c.entity_key, 0) >= min_beats]
    voices = assign_voices([c.entity_key for c in principals])

    keyframe_keys: dict[str, list[str]] = {}
    for entity in sorted(principals, key=lambda c: c.entity_key):
        prompt = _keyframe_prompt(entity, style_tokens)
        ref_descriptors: list[dict[str, Any]] = []
        produced: list[str] = []
        for pose_index, pose in enumerate(poses):
            images = await providers.image.generate(
                prompt,
                size=keyframe_size,
                n=1,
                negative_prompt=_KEYFRAME_NEGATIVE,
                seed=_seed_for(entity.entity_key, pose_index),
            )
            if not images:
                continue
            ref_key = keys.ref(book_id, entity.entity_key, f"ref_{pose}.png")
            await anyio.to_thread.run_sync(
                blob_store.put_bytes, ref_key, images[0], _PNG_CONTENT_TYPE
            )
            ref_descriptors.append({"key": ref_key, "pose": pose, "locked": True})
            produced.append(ref_key)

        if not ref_descriptors:
            logger.warning("ingest.identity.no_keyframe", entity_key=entity.entity_key)
            continue

        voice_id = voices[entity.entity_key]
        # Re-upsert the entity with LOCKED refs (canon computes the embedding) and
        # its assigned preset voice — a new version locking appearance + voice.
        await canon.upsert_entity(
            book_id=book_id,
            entity_key=entity.entity_key,
            entity_type=EntityType.CHARACTER,
            name=entity.name,
            valid_from_beat=1,
            aliases=entity.aliases or None,
            description=entity.description or None,
            appearance={
                "description": entity.description,
                "reference_images": ref_descriptors,
                "locked": True,
            },
            voice=_voice_record(voice_id, role="character"),
            first_appearance={"page": entity.first_page},
        )
        keyframe_keys[entity.entity_key] = produced

    # The narrator: a dedicated entity carrying the reserved narration voice. It is
    # in no beat, so it is never surfaced by canon.query nor treated as a principal.
    await canon.upsert_entity(
        book_id=book_id,
        entity_key=NARRATOR_ENTITY_KEY,
        entity_type=EntityType.CHARACTER,
        name="Narrator",
        valid_from_beat=1,
        voice=_voice_record(NARRATOR_VOICE.voice_id, role="narrator"),
    )
    voices[NARRATOR_ENTITY_KEY] = NARRATOR_VOICE.voice_id

    result = IdentityLockResult(
        book_id=book_id,
        principals=[c.entity_key for c in principals],
        voices=voices,
        narrator_voice=NARRATOR_VOICE.voice_id,
        keyframe_keys=keyframe_keys,
    )
    logger.info(
        "ingest.identity.done",
        book_id=book_id,
        principals=len(result.principals),
        keyframes=sum(len(v) for v in keyframe_keys.values()),
    )
    return result


def _voice_record(voice_id: str, *, role: str) -> dict[str, Any]:
    """Build the entity ``voice`` JSON (preset id + params; never a clone)."""
    return {
        "voice_id": voice_id,
        "preset": True,
        "model": PRESET_TTS_MODEL,
        "role": role,
        "params": {"speed": 1.0, "pitch": 1.0},
    }


__all__ = [
    "NARRATOR_ENTITY_KEY",
    "NARRATOR_VOICE",
    "PRESET_VOICES",
    "PRESET_TTS_MODEL",
    "IdentityLockResult",
    "PresetVoice",
    "assign_voices",
    "lock_identities",
]
