"""The cached render artifact — what a clip-cache hit serves.

A :class:`ClipRecord` is the small, JSON-serialisable *metadata* about a rendered
clip: where its bytes live in object storage, its companion last frame, the
duration, and provenance (provider/model, the video-seconds it cost, when it was
rendered, and which books have referenced it — for cross-book reuse accounting).
The clip *bytes* themselves stay in object storage; the cache stores only this
pointer, so promoting a record between tiers is cheap.

A hit serves this record and spends **zero** video-seconds — the whole point of
the dedup layer (kinora.md §8.7 / §11.1).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClipRecord(BaseModel):
    """Metadata for a rendered clip, keyed by its content-addressed render key."""

    model_config = ConfigDict(extra="ignore")

    #: The ``RenderKey.value`` this record is stored under (content address).
    render_key: str
    #: Object-store key for the rendered clip bytes (e.g. ``clips/cache/<short>.mp4``).
    clip_key: str
    #: Object-store key for the clip's last frame (continuation anchor), if any.
    last_frame_key: str | None = None
    #: Clip duration in seconds (post-quantisation it equals the requested grid value).
    duration_s: float = 0.0
    #: Provider + model that produced the clip (provenance / cost attribution).
    provider: str = ""
    model: str = ""
    #: Video-seconds the *original* render charged. A cache hit re-charges 0; this
    #: is retained so a dashboard can total the seconds the cache *saved*.
    video_seconds: float = 0.0
    #: Wall-clock epoch seconds when the clip was first rendered (for TTL / audit).
    created_at: float = 0.0
    #: Books that have reused this clip (set semantics; cross-book reuse evidence).
    #: Stored as a sorted list for JSON stability.
    referencing_books: list[str] = Field(default_factory=list)
    #: Logical invalidation tags this clip depends on (e.g. ``entity:42``,
    #: ``book:7``). Stored *on the record* so a reuse-driven re-persist can
    #: re-apply the same tags without double-qualifying the facade's tag keys.
    tags: list[str] = Field(default_factory=list)
    #: Free-form QA / sync metadata carried from the render pipeline.
    qa: dict[str, Any] | None = None
    sync_segment: dict[str, Any] | None = None

    def with_book(self, book_id: str | None) -> ClipRecord:
        """Return a copy that records ``book_id`` as a referencing book (idempotent)."""
        if not book_id or book_id in self.referencing_books:
            return self
        return self.model_copy(
            update={"referencing_books": sorted({*self.referencing_books, book_id})}
        )

    @property
    def reuse_count(self) -> int:
        """How many distinct books have referenced this clip."""
        return len(self.referencing_books)


class ClipLookup(BaseModel):
    """The typed outcome of a clip-cache probe.

    Mirrors the shape of :class:`app.memory.cache_service.CacheLookup` so call
    sites can treat a content-addressed hit identically to a §8.7 shot-cache hit:
    a hit carries the record + presigned URLs and charges ``video_seconds == 0``.
    """

    model_config = ConfigDict(extra="ignore")

    hit: bool
    render_key: str
    tier: str | None = None  # "l1" | "l2" | "object" on a hit; None on a miss
    record: ClipRecord | None = None
    clip_url: str | None = None
    last_frame_url: str | None = None
    #: Always 0 on a hit (the saving); echoes the record's cost on the producing path.
    video_seconds: float = 0.0
    #: Video-seconds this hit *avoided* re-spending (== record.video_seconds on a hit).
    video_seconds_saved: float = 0.0


__all__ = ["ClipLookup", "ClipRecord"]
