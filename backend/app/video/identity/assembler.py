"""Assemble an :class:`IdentityBundle` from a canon entity slice (the real seam).

The canon graph (kinora.md §8.1) is the source of locked identity:
``canon.query`` / ``canon.get_entity`` return a
:class:`~app.memory.interfaces.CanonEntitySlice` carrying the entity's locked
reference images (with presigned URLs + poses), its appearance description /
embedding, optional voice ref, and style tokens. This module is the pure adapter
that projects that canon shape into the provider-agnostic
:class:`~app.video.identity.bundle.IdentityBundle` the conditioner consumes.

It is deliberately tolerant: a slice missing embeddings, poses, or URLs still
yields a usable bundle (the conditioner + keyframe fallback degrade gracefully).
No I/O — the caller fetches bytes/URLs; this just maps fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.memory.interfaces import CanonEntitySlice, RefImage

from .bundle import IdentityBundle, LockedReference, Pose

#: Canon ``appearance`` keys that may carry the appearance embedding vector.
_EMBEDDING_KEYS = ("embedding", "appearance_embedding", "vector")
#: Canon ``appearance`` keys that may carry the short locked appearance phrase.
_PHRASE_KEYS = ("description", "appearance_prompt", "phrase", "locked_phrase")


def _as_float_tuple(value: Any) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    out: list[float] = []
    for v in value:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
        else:
            return ()
    return tuple(out)


def _first(mapping: Mapping[str, Any] | None, keys: Sequence[str]) -> Any:
    if not mapping:
        return None
    for k in keys:
        if k in mapping and mapping[k] is not None:
            return mapping[k]
    return None


def _negative_tokens(appearance: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not appearance:
        return ()
    raw = appearance.get("negative_tokens") or appearance.get("negatives")
    if isinstance(raw, str):
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    return ()


def locked_reference_from_ref_image(
    ref: RefImage,
    *,
    entity_key: str,
    version: int,
    image_bytes: bytes | None = None,
    descriptor: tuple[float, ...] = (),
    quality: float = 1.0,
) -> LockedReference:
    """Map one canon :class:`RefImage` to a :class:`LockedReference` (pure).

    ``image_bytes`` / ``descriptor`` are supplied by the caller when it has already
    fetched/embedded the ref (the canon slice itself carries only keys + URLs). A
    stable ``ref_id`` is derived from the entity key + version + canon key/pose.
    """
    pose = Pose.coerce(ref.pose)
    ref_id = f"{entity_key}@v{version}:{ref.key or pose.value}"
    return LockedReference(
        ref_id=ref_id,
        pose=pose,
        url=ref.url,
        image_bytes=image_bytes,
        descriptor=descriptor,
        locked=ref.locked,
        quality=quality,
    )


def bundle_from_canon_slice(
    slice_: CanonEntitySlice,
    *,
    ref_bytes: Mapping[str, bytes] | None = None,
    ref_descriptors: Mapping[str, tuple[float, ...]] | None = None,
) -> IdentityBundle:
    """Project a :class:`CanonEntitySlice` into an :class:`IdentityBundle` (pure).

    ``ref_bytes`` / ``ref_descriptors`` are keyed by the canon ``RefImage.key`` and
    let the caller attach fetched pixels / embeddings to the right ref (both
    optional — a slice with neither still yields a URL-only bundle).
    """
    ref_bytes = ref_bytes or {}
    ref_descriptors = ref_descriptors or {}
    appearance = slice_.appearance or {}

    references: list[LockedReference] = []
    for ref in slice_.reference_images:
        references.append(
            locked_reference_from_ref_image(
                ref,
                entity_key=slice_.entity_key,
                version=slice_.version,
                image_bytes=ref_bytes.get(ref.key),
                descriptor=ref_descriptors.get(ref.key, ()),
            )
        )

    appearance_descriptor = _as_float_tuple(_first(appearance, _EMBEDDING_KEYS))
    phrase = _first(appearance, _PHRASE_KEYS)
    appearance_prompt = str(phrase).strip() if phrase else (slice_.description or "")

    character_id: str | None = None
    if isinstance(appearance, Mapping):
        cid = appearance.get("character_id") or appearance.get("subject_id")
        if cid:
            character_id = str(cid)

    return IdentityBundle(
        entity_key=slice_.entity_key,
        entity_type=slice_.type,
        name=slice_.name,
        version=slice_.version,
        references=tuple(references),
        appearance_descriptor=appearance_descriptor,
        character_id=character_id,
        appearance_prompt=appearance_prompt,
        negative_tokens=_negative_tokens(appearance),
        voice_ref_url=slice_.voice_ref_url,
    )


__all__ = [
    "bundle_from_canon_slice",
    "locked_reference_from_ref_image",
]
