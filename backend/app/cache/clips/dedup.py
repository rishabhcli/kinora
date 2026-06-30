"""``RenderCache`` — the typed clip dedup facade the render path talks to.

This is the public object the render pipeline / scheduler use. It turns the
generic, dict-storing :class:`~app.cache.cache.Cache` into a typed clip cache:

* **Content-addressed get/put** keyed by :class:`~app.cache.clips.keys.RenderKey`
  (so semantically-identical renders collide across books and sessions).
* **`get_or_render` — singleflight / request coalescing.** When N concurrent
  callers all request the *same* render key and it is a miss, exactly one runs the
  expensive ``render`` coroutine; the rest await and share its
  :class:`~app.cache.clips.record.ClipRecord`. This is what stops two sessions (or
  two books) that hit the same shot at the same instant from *both* paying for it.
  Cancellation of a follower never cancels the shared leader (inherited from
  :class:`~app.cache.singleflight.SingleFlight` via the facade).
* **Cross-book / cross-session reuse accounting.** Each hit records the requesting
  book on the record, so a dashboard can see how many books a single rendered clip
  served (and total the video-seconds the dedup layer saved).
* **Tag-based invalidation** — a clip can be tagged by the entities it depends on
  (``entity:<id>``) so a Director edit that changes one character drops only the
  clips that referenced it (the §8.7 cheap-edit story, generalised cross-book).
* **Warmup / prefetch hook** the scheduler can call ahead of the read cursor.
* **Stats** — hit/miss/eviction + the bespoke ``video_seconds_saved`` total.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence

from app.cache.cache import Cache, CacheConfig
from app.cache.clips.keys import RenderInputs, RenderKey
from app.cache.clips.record import ClipLookup, ClipRecord
from app.cache.clips.store import ClipBlobStore
from app.cache.clips.tiers import build_clip_cache
from app.cache.clock import SYSTEM_CLOCK, Clock
from app.cache.metrics import CacheMetrics

#: Default namespace for the clip dedup cache.
CLIP_NAMESPACE = "clipcache"

#: Default TTL for a cached clip record. Long-lived: a rendered clip is durable in
#: object storage, so the *record* only needs to outlive the read window. ``None``
#: on the durable tier means it never auto-expires there.
DEFAULT_CLIP_TTL_S = 7 * 24 * 3600.0


def _default_config(namespace: str, ttl: float | None) -> CacheConfig:
    return CacheConfig(
        namespace=namespace,
        default_ttl=ttl,
        # Negative caching is OFF for clips: a "not rendered yet" is the normal
        # state and must never be cached as an absence (that would suppress the
        # render). The render path decides; the cache only remembers *successes*.
        cache_negatives=False,
        # Records are immutable for a given content address, so early-expiry
        # stampede smoothing buys nothing and would cause needless re-renders.
        early_expiry_beta=0.0,
        fail_open=True,
    )


class RenderCache:
    """Typed, multi-tier, dedup-aware cache for rendered clips."""

    def __init__(
        self,
        cache: Cache[dict],
        *,
        blob_store: ClipBlobStore | None = None,
        url_ttl: int = 3600,
        record_ttl_s: float | None = DEFAULT_CLIP_TTL_S,
    ) -> None:
        self._cache = cache
        self._store = blob_store
        self._url_ttl = url_ttl
        self._record_ttl = record_ttl_s
        # Bespoke counter the generic CacheMetrics doesn't model: video-seconds the
        # dedup layer avoided re-spending. Guarded for thread/async safety.
        self._saved_lock = threading.Lock()
        self._video_seconds_saved = 0.0
        self._cross_book_hits = 0

    # --- construction --- #

    @classmethod
    def build(
        cls,
        *,
        namespace: str = CLIP_NAMESPACE,
        clock: Clock | None = None,
        metrics: CacheMetrics | None = None,
        l1_max_entries: int = 2048,
        redis: object | None = None,
        object_store: ClipBlobStore | None = None,
        url_ttl: int = 3600,
        record_ttl_s: float | None = DEFAULT_CLIP_TTL_S,
    ) -> RenderCache:
        """One-call construction of the whole tier stack + typed facade.

        With no ``redis`` and no ``object_store`` this is a pure in-process cache
        (the no-infra default for tests). ``object_store`` enables durable,
        fleet-wide reuse; ``redis`` enables the cross-process L2 tier.
        """
        cache = build_clip_cache(
            namespace=namespace,
            config=_default_config(namespace, record_ttl_s),
            clock=clock or SYSTEM_CLOCK,
            metrics=metrics,
            l1_max_entries=l1_max_entries,
            redis=redis,
            object_store=object_store,
        )
        return cls(
            cache, blob_store=object_store, url_ttl=url_ttl, record_ttl_s=record_ttl_s
        )

    @property
    def namespace(self) -> str:
        return self._cache.namespace

    # --- typed get / put --- #

    async def get(
        self, key: RenderKey | RenderInputs, *, book_id: str | None = None
    ) -> ClipLookup:
        """Probe the cache for ``key``; return a typed hit or miss.

        A hit charges **0** video-seconds and records ``book_id`` as a referencing
        book (cross-book reuse evidence). ``key`` may be a :class:`RenderKey` or a
        :class:`RenderInputs` (its key is derived).
        """
        render_key = key.key() if isinstance(key, RenderInputs) else key
        raw = await self._cache.get(render_key.value)
        if raw is None:
            return ClipLookup(hit=False, render_key=render_key.value)
        record = ClipRecord.model_validate(raw)
        await self._note_reuse(render_key, record, book_id)
        return self._hit(render_key, record)

    async def put(
        self,
        key: RenderKey | RenderInputs,
        record: ClipRecord,
        *,
        tags: Iterable[str] | None = None,
        ttl: float | None = None,
    ) -> ClipRecord:
        """Store a freshly-rendered clip's record under its content address.

        Future identical renders (any book, any session) then hit instead of
        paying again. ``tags`` (e.g. ``["entity:42", "book:7"]``) enable targeted
        invalidation.
        """
        render_key = key.key() if isinstance(key, RenderInputs) else key
        tag_list = sorted(set(tags)) if tags is not None else list(record.tags)
        record = record.model_copy(
            update={"render_key": render_key.value, "tags": tag_list}
        )
        effective_ttl = self._record_ttl if ttl is None else ttl
        await self._cache.set(
            render_key.value, record.model_dump(mode="json"), ttl=effective_ttl, tags=tag_list
        )
        return record

    # --- read-through with singleflight coalescing --- #

    async def get_or_render(
        self,
        key: RenderKey | RenderInputs,
        render: Callable[[], Awaitable[ClipRecord]],
        *,
        book_id: str | None = None,
        tags: Iterable[str] | None = None,
        ttl: float | None = None,
    ) -> ClipLookup:
        """Serve a cached clip, or run ``render`` exactly once for a wave of callers.

        On a miss, ``render`` (the expensive provider round-trip) is executed under
        single-flight: concurrent callers for the *same* render key share the one
        execution and its resulting record — so identical renders requested
        simultaneously across sessions/books are paid for once. The produced record
        is then cached for subsequent waves.

        Returns a :class:`ClipLookup`: ``hit=True`` when served from cache, ``hit
        =False`` when this caller's wave produced it (``video_seconds`` then echoes
        the render's cost rather than the saving).
        """
        render_key = key.key() if isinstance(key, RenderInputs) else key
        effective_ttl = self._record_ttl if ttl is None else ttl

        # Probe first so a genuine hit short-circuits before single-flight (and so
        # we can record cross-book reuse on the hit path).
        probe = await self.get(render_key, book_id=book_id)
        if probe.hit:
            return probe

        produced: dict[str, ClipRecord] = {}

        tag_list = sorted(set(tags)) if tags is not None else None

        async def _loader() -> dict:
            record = await render()
            update: dict[str, object] = {"render_key": render_key.value}
            if tag_list is not None:
                update["tags"] = tag_list
            record = record.model_copy(update=update)
            record = record.with_book(book_id)
            produced["record"] = record
            return record.model_dump(mode="json")

        raw = await self._cache.get_or_load(
            render_key.value, _loader, ttl=effective_ttl, tags=tag_list
        )
        record = ClipRecord.model_validate(raw)

        # The leader of this wave produced the record (paid the render); followers
        # got it from single-flight (a coalesced share — still counts as a hit-like
        # saving but is reported per-caller as a non-hit since they didn't read the
        # persisted cache). We distinguish by whether *this* call ran the loader.
        if "record" in produced:
            # This caller (the leader) actually rendered.
            return ClipLookup(
                hit=False,
                render_key=render_key.value,
                tier=None,
                record=record,
                clip_url=self._presign(record.clip_key),
                last_frame_url=self._presign(record.last_frame_key),
                video_seconds=record.video_seconds,
                video_seconds_saved=0.0,
            )
        # A follower shared the leader's result: it paid nothing — count the saving.
        await self._note_reuse(render_key, record, book_id, coalesced=True)
        return self._hit(render_key, record, tier="coalesced")

    # --- cross-book reuse accounting --- #

    async def _note_reuse(
        self,
        key: RenderKey,
        record: ClipRecord,
        book_id: str | None,
        *,
        coalesced: bool = False,
    ) -> None:
        """Record that ``book_id`` reused ``record``; persist the updated book set."""
        with self._saved_lock:
            self._video_seconds_saved += record.video_seconds
        if not book_id:
            return
        if book_id not in record.referencing_books:
            with self._saved_lock:
                self._cross_book_hits += 1
            updated = record.with_book(book_id)
            # Persist the widened referencing-book set so reuse evidence survives,
            # re-applying the record's own tags (kept on the record) so the
            # re-persist never strips the entry's dependency tags.
            await self._cache.set(
                key.value,
                updated.model_dump(mode="json"),
                ttl=self._record_ttl,
                tags=updated.tags or None,
            )

    # --- invalidation --- #

    async def invalidate(self, *keys: RenderKey | RenderInputs) -> int:
        """Drop one or more clips by content address (key-based invalidation)."""
        values = [(k.key() if isinstance(k, RenderInputs) else k).value for k in keys]
        return await self._cache.invalidate(*values)

    async def invalidate_tag(self, tag: str) -> int:
        """Drop every clip carrying ``tag`` (the §8.7 cheap-edit primitive)."""
        return await self._cache.invalidate_tag(tag)

    # --- warmup / prefetch --- #

    async def warm(
        self,
        items: Sequence[tuple[RenderKey | RenderInputs, Callable[[], Awaitable[ClipRecord]]]],
        *,
        book_id: str | None = None,
        tags: Iterable[str] | None = None,
        skip_present: bool = True,
    ) -> int:
        """Prefetch a batch of clips ahead of the read cursor (scheduler hook).

        For each ``(key, render)`` pair: if a record is already cached and
        ``skip_present`` is set, nothing happens (no re-render); otherwise the
        clip is rendered via the dedup path (so concurrent warmups of the same key
        still coalesce). Returns the number of *new* renders triggered. Intended to
        be called for the speculative zone so the committed read window is already
        warm.
        """
        rendered = 0
        for key, render in items:
            render_key = key.key() if isinstance(key, RenderInputs) else key
            if skip_present and await self._cache.has(render_key.value):
                continue
            result = await self.get_or_render(
                render_key, render, book_id=book_id, tags=tags
            )
            if not result.hit and result.tier is None:
                rendered += 1
        return rendered

    async def prefetch_keys(self, keys: Iterable[RenderKey | RenderInputs]) -> dict[str, bool]:
        """Pull a set of keys into the fast tiers and report presence.

        A pure read-side warmup: probing each key promotes any L2/L3 hit up into
        L1 (the tiered backend promotes on read). Returns ``{key_value: present}``.
        """
        out: dict[str, bool] = {}
        for key in keys:
            render_key = key.key() if isinstance(key, RenderInputs) else key
            out[render_key.value] = await self._cache.has(render_key.value)
        return out

    # --- stats --- #

    def stats(self) -> dict[str, float | int | str]:
        """Per-namespace counters plus the clip-specific saved-seconds totals."""
        base = self._cache.stats().as_dict()
        with self._saved_lock:
            base["video_seconds_saved"] = round(self._video_seconds_saved, 6)
            base["cross_book_hits"] = self._cross_book_hits
        return base

    @property
    def video_seconds_saved(self) -> float:
        with self._saved_lock:
            return self._video_seconds_saved

    @property
    def cross_book_hits(self) -> int:
        with self._saved_lock:
            return self._cross_book_hits

    async def health(self) -> bool:
        return await self._cache.health()

    async def close(self) -> None:
        await self._cache.close()

    # --- internals --- #

    def _hit(self, key: RenderKey, record: ClipRecord, *, tier: str | None = None) -> ClipLookup:
        return ClipLookup(
            hit=True,
            render_key=key.value,
            tier=tier,
            record=record,
            clip_url=self._presign(record.clip_key),
            last_frame_url=self._presign(record.last_frame_key),
            video_seconds=0.0,
            video_seconds_saved=record.video_seconds,
        )

    def _presign(self, object_key: str | None) -> str | None:
        if object_key is None or self._store is None:
            return None
        return self._store.presigned_get_url(object_key, ttl=self._url_ttl)


__all__ = ["CLIP_NAMESPACE", "DEFAULT_CLIP_TTL_S", "RenderCache"]
