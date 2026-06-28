"""The :class:`MediaAssetKind` taxonomy.

Shared by the ORM model, the repository, the service, and the API so the kind of
every managed blob is a closed, typed set rather than a free string. Stored as a
short string column (``str_enum``) so values are stable and human-readable in the
database.
"""

from __future__ import annotations

from enum import StrEnum


class MediaAssetKind(StrEnum):
    """What a managed media blob *is* (drives derivation + retention)."""

    #: A rendered shot clip (mp4). Complements §9.7 — already persisted by the
    #: render pipeline; we track metadata + derive posters/sprites/HLS from it.
    CLIP = "clip"
    #: A stitched scene/event film (mp4) — the §9.6 stitch output.
    SCENE = "scene"
    #: A single poster still (the representative frame for a clip/scene).
    POSTER = "poster"
    #: A small thumbnail still (shelf / scrubber preview).
    THUMBNAIL = "thumbnail"
    #: A sprite sheet of evenly-spaced frames (+ a sibling WEBVTT).
    SPRITE = "sprite"
    #: A WEBVTT cue file (sprite thumbnails or chapters).
    VTT = "vtt"
    #: An HLS variant playlist or media segment.
    HLS = "hls"
    #: A DASH manifest or media segment.
    DASH = "dash"
    #: Narration / mixed audio (wav/m4a).
    AUDIO = "audio"
    #: A locked keyframe / reference still (image-gen, §9.1).
    KEYFRAME = "keyframe"
    #: The source document (PDF/EPUB) or other ingest input.
    SOURCE = "source"
    #: Anything else under management.
    OTHER = "other"


#: Kinds that are *derived* from a primary asset and are therefore safe to
#: regenerate — the lifecycle GC may collect these aggressively when orphaned.
DERIVED_KINDS: frozenset[MediaAssetKind] = frozenset(
    {
        MediaAssetKind.POSTER,
        MediaAssetKind.THUMBNAIL,
        MediaAssetKind.SPRITE,
        MediaAssetKind.VTT,
        MediaAssetKind.HLS,
        MediaAssetKind.DASH,
    }
)

#: Kinds that are *primary* — expensive to (re)produce; never collected as
#: "merely derived". Their removal is governed by explicit retention only.
PRIMARY_KINDS: frozenset[MediaAssetKind] = frozenset(
    {
        MediaAssetKind.CLIP,
        MediaAssetKind.SCENE,
        MediaAssetKind.AUDIO,
        MediaAssetKind.KEYFRAME,
        MediaAssetKind.SOURCE,
    }
)


def is_derived(kind: MediaAssetKind) -> bool:
    """True when ``kind`` is a regenerable derivative (poster/sprite/HLS/…)."""
    return kind in DERIVED_KINDS


__all__ = [
    "DERIVED_KINDS",
    "PRIMARY_KINDS",
    "MediaAssetKind",
    "is_derived",
]
