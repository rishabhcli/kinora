"""MediaService — the orchestration facade for the media subsystem.

Ties the pieces together so callers (the render pipeline's persistence step, a
future transcode worker, the API) have one verb-shaped surface:

* :meth:`store_asset` — content-addressed put + register a :class:`MediaAsset`
  row (byte-level dedup; complements §8.7's shot-hash dedup).
* :meth:`ingest_clip` — register an existing clip (probe geometry/duration).
* :meth:`make_poster` / :meth:`make_thumbnail` / :meth:`make_sprite` — derive a
  still / scrubber sprite + WEBVTT from a stored clip, store + register them.
* :meth:`package_film` — segment a clip into HLS (master + variants) and upload
  the whole package under one prefix, registering the entry-point playlist.
* :meth:`run_transcode_job` — execute a :class:`TranscodeJob`'s derivations.
* :meth:`url_for` — a browser-reachable URL for a key.
* :meth:`gc` — run the lifecycle sweep.

Persistence is optional: with no ``session_factory`` the service still stores +
derives (returning :class:`AssetMetadata`), it just does not record rows — so it
is usable in a thin worker that only needs the bytes. All ffmpeg/boto3 calls are
blocking and run through ``anyio.to_thread`` so the service is await-friendly.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.media.kinds import MediaAssetKind
from app.media.lifecycle import (
    IntegrityReport,
    RetentionPolicy,
    SweepResult,
    sweep_expired,
    verify_integrity,
)
from app.media.metadata import AssetMetadata, suffix_for
from app.media.repository import MediaAssetRepo
from app.media.store import MediaStore
from app.media.transcode import Derivation, TranscodeJob
from app.media.vtt import sprite_vtt

logger = get_logger("app.media.service")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class MediaService:
    """Content-addressed storage + ffmpeg derivations + packaging + GC."""

    def __init__(
        self,
        store: MediaStore,
        *,
        session_factory: SessionFactory | None = None,
        retention: RetentionPolicy | None = None,
        url_ttl_s: int = 3600,
        segment_s: int = 4,
        sprite_count: int = 20,
        gc_batch: int = 100,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._retention = retention or RetentionPolicy()
        self._url_ttl_s = url_ttl_s
        self._segment_s = segment_s
        self._sprite_count = sprite_count
        self._gc_batch = gc_batch

    @property
    def store(self) -> MediaStore:
        """The underlying content-addressed store."""
        return self._store

    # -- registration ------------------------------------------------------- #

    async def _register(
        self, meta: AssetMetadata, *, extra_meta: dict[str, Any] | None = None
    ) -> AssetMetadata:
        """Persist a row for ``meta`` when a session factory is configured."""
        if self._session_factory is None:
            return meta
        now = datetime.now(UTC)
        expires = self._retention.expires_at(meta.kind, now=now)
        async with self._session_factory() as session:
            await MediaAssetRepo(session).register(
                meta, expires_at=expires, extra_meta=extra_meta
            )
        return meta

    # -- storing ------------------------------------------------------------- #

    async def store_asset(
        self,
        data: bytes,
        *,
        kind: MediaAssetKind,
        content_type: str | None = None,
        suffix: str | None = None,
        book_id: str | None = None,
        prefix: str | None = None,
    ) -> AssetMetadata:
        """Content-addressed store + register (byte-level dedup)."""
        ext = suffix if suffix is not None else (suffix_for(content_type or "") or "")
        meta, dedup = await anyio.to_thread.run_sync(
            lambda: self._store.put_content_addressed(
                data,
                suffix=ext,
                content_type=content_type,
                kind=kind,
                prefix=prefix,
                book_id=book_id,
            )
        )
        await self._register(meta, extra_meta={"deduplicated": dedup})
        logger.info("media.store", kind=str(kind), key=meta.storage_key, dedup=dedup)
        return meta

    async def ingest_clip(
        self,
        clip_bytes: bytes,
        *,
        kind: MediaAssetKind = MediaAssetKind.CLIP,
        book_id: str | None = None,
        prefix: str | None = None,
    ) -> AssetMetadata:
        """Store a clip content-addressed and record its probed AV metadata."""
        from app.media.probe import metadata_for

        key = self._store.address_of(clip_bytes, suffix=".mp4", prefix=prefix)
        probed = await anyio.to_thread.run_sync(
            lambda: metadata_for(
                clip_bytes, storage_key=key, kind=kind, content_type="video/mp4", book_id=book_id
            )
        )
        await anyio.to_thread.run_sync(
            lambda: self._store.put_content_addressed(
                clip_bytes,
                suffix=".mp4",
                content_type="video/mp4",
                kind=kind,
                prefix=prefix,
                book_id=book_id,
            )
        )
        await self._register(probed)
        return probed

    # -- derivations --------------------------------------------------------- #

    async def make_poster(
        self,
        clip_bytes: bytes,
        *,
        at_s: float | None = None,
        width: int | None = None,
        book_id: str | None = None,
    ) -> AssetMetadata:
        """Derive + store a poster still (PNG) for a clip."""
        from app.media.images import extract_poster

        png = await anyio.to_thread.run_sync(
            lambda: extract_poster(clip_bytes, at_s=at_s, width=width)
        )
        return await self.store_asset(
            png, kind=MediaAssetKind.POSTER, content_type="image/png", book_id=book_id
        )

    async def make_thumbnail(
        self,
        clip_bytes: bytes,
        *,
        at_s: float | None = None,
        width: int = 320,
        book_id: str | None = None,
    ) -> AssetMetadata:
        """Derive + store a small thumbnail still (PNG) for a clip."""
        from app.media.images import extract_thumbnail

        png = await anyio.to_thread.run_sync(
            lambda: extract_thumbnail(clip_bytes, at_s=at_s, width=width)
        )
        return await self.store_asset(
            png, kind=MediaAssetKind.THUMBNAIL, content_type="image/png", book_id=book_id
        )

    async def make_sprite(
        self,
        clip_bytes: bytes,
        *,
        count: int | None = None,
        book_id: str | None = None,
    ) -> tuple[AssetMetadata, AssetMetadata]:
        """Derive a sprite sheet + its WEBVTT; store + register both.

        Returns ``(sheet_meta, vtt_meta)``. The VTT references the sheet via the
        sheet's browser URL so the player can resolve tiles directly.
        """
        from app.media.images import build_sprite_sheet

        n = count or self._sprite_count
        sheet = await anyio.to_thread.run_sync(lambda: build_sprite_sheet(clip_bytes, count=n))
        sheet_meta = await self.store_asset(
            sheet.image,
            kind=MediaAssetKind.SPRITE,
            content_type="image/png",
            book_id=book_id,
        )
        sheet_url = await self.url_for(sheet_meta.storage_key)
        vtt_text = sprite_vtt(
            sprite_url=sheet_url,
            columns=sheet.columns,
            rows=sheet.rows,
            tile_width=sheet.tile_width,
            tile_height=sheet.tile_height,
            tile_count=sheet.tile_count,
            interval_s=sheet.interval_s,
        )
        vtt_meta = await self.store_asset(
            vtt_text.encode("utf-8"),
            kind=MediaAssetKind.VTT,
            content_type="text/vtt",
            book_id=book_id,
        )
        return sheet_meta, vtt_meta

    # -- packaging ----------------------------------------------------------- #

    async def package_film(
        self,
        clip_bytes: bytes,
        *,
        book_id: str | None = None,
        prefix: str | None = None,
    ) -> AssetMetadata:
        """Segment a clip into HLS and upload the whole package under a prefix.

        Every file (master playlist, variant playlists, segments) is uploaded
        under ``<prefix or content-address>/`` with its own content type; the
        returned metadata is the **master playlist** entry-point, with the
        variant list + file map recorded in ``meta`` for the registry.
        """
        from app.media.packaging import package_hls

        result = await anyio.to_thread.run_sync(
            lambda: package_hls(clip_bytes, segment_s=self._segment_s)
        )
        # Anchor the package at the clip's content address so re-packaging dedups.
        base = (prefix or self._store.address_of(clip_bytes, prefix=None)).rstrip("/")
        base = f"{base}-hls"
        from app.media.metadata import guess_content_type

        master_key = f"{base}/{result.entrypoint}"
        for relpath, payload in result.files.items():
            key = f"{base}/{relpath}"
            ctype = guess_content_type(relpath)
            await anyio.to_thread.run_sync(self._store.put, key, payload, ctype)
        master = AssetMetadata(
            storage_key=master_key,
            kind=MediaAssetKind.HLS,
            content_type=guess_content_type(result.entrypoint),
            size_bytes=len(result.files[result.entrypoint]),
            book_id=book_id,
            meta={
                "variants": list(result.variants),
                "segment_count": result.segment_count,
                "files": sorted(result.files),
            },
        )
        await self._register(master)
        logger.info(
            "media.package", key=master_key, variants=list(result.variants),
            files=len(result.files),
        )
        return master

    # -- transcode jobs ------------------------------------------------------ #

    async def run_transcode_job(self, job: TranscodeJob) -> dict[Derivation, AssetMetadata]:
        """Execute a job's derivations against its source clip; return results.

        Pulls the source bytes from the store (the §9.7 pipeline already
        persisted them), runs each requested derivation, and returns a map of
        derivation → produced asset. The sprite derivation contributes its sheet
        (its sibling VTT is also stored).
        """
        source = await anyio.to_thread.run_sync(self._store.get, job.source_key)
        out: dict[Derivation, AssetMetadata] = {}
        for deriv in job.derivations:
            if deriv is Derivation.POSTER:
                out[deriv] = await self.make_poster(source, book_id=job.book_id)
            elif deriv is Derivation.THUMBNAIL:
                out[deriv] = await self.make_thumbnail(source, book_id=job.book_id)
            elif deriv is Derivation.SPRITE:
                sheet, _vtt = await self.make_sprite(source, book_id=job.book_id)
                out[deriv] = sheet
            elif deriv is Derivation.HLS:
                out[deriv] = await self.package_film(source, book_id=job.book_id)
            elif deriv is Derivation.DASH:
                out[deriv] = await self._package_dash(source, book_id=job.book_id)
        logger.info("media.transcode", job=job.job_id, derived=[str(d) for d in out])
        return out

    async def _package_dash(
        self, clip_bytes: bytes, *, book_id: str | None
    ) -> AssetMetadata:
        from app.media.metadata import guess_content_type
        from app.media.packaging import package_dash

        result = await anyio.to_thread.run_sync(
            lambda: package_dash(clip_bytes, segment_s=self._segment_s)
        )
        base = f"{self._store.address_of(clip_bytes).rstrip('/')}-dash"
        manifest_key = f"{base}/{result.entrypoint}"
        for relpath, payload in result.files.items():
            await anyio.to_thread.run_sync(
                self._store.put, f"{base}/{relpath}", payload, guess_content_type(relpath)
            )
        manifest = AssetMetadata(
            storage_key=manifest_key,
            kind=MediaAssetKind.DASH,
            content_type=guess_content_type(result.entrypoint),
            size_bytes=len(result.files[result.entrypoint]),
            book_id=book_id,
            meta={"segment_count": result.segment_count, "files": sorted(result.files)},
        )
        await self._register(manifest)
        return manifest

    # -- urls + gc ----------------------------------------------------------- #

    async def url_for(self, key: str, *, ttl: int | None = None) -> str:
        """A browser-reachable URL for ``key`` (public base else signed)."""
        return await anyio.to_thread.run_sync(
            lambda: self._store.url_for(key, ttl=ttl if ttl is not None else self._url_ttl_s)
        )

    async def gc(self, *, now: datetime | None = None) -> SweepResult:
        """Run the lifecycle sweep (no-op without a session factory)."""
        if self._session_factory is None:
            return SweepResult(collected=0, bytes_freed=0, keys=())
        when = now or datetime.now(UTC)
        async with self._session_factory() as session:
            return await sweep_expired(
                MediaAssetRepo(session), self._store, now=when, batch=self._gc_batch
            )

    async def verify_book_integrity(
        self, book_id: str, *, kind: MediaAssetKind | None = None
    ) -> IntegrityReport:
        """Re-hash a book's stored blobs vs the registry (no-op without DB)."""
        if self._session_factory is None:
            return IntegrityReport(checked=0, ok=0, missing=(), corrupt=())
        async with self._session_factory() as session:
            return await verify_integrity(
                MediaAssetRepo(session), self._store, book_id=book_id, kind=kind
            )


def build_media_service(
    settings: Any,
    *,
    object_store: Any,
    session_factory: SessionFactory | None = None,
) -> MediaService:
    """Wire a :class:`MediaService` from application settings + an object store.

    Used by the composition root. ``object_store`` is the existing
    :class:`app.storage.object_store.ObjectStore` (it satisfies the store
    backend Protocol), so the media layer reuses one client.
    """
    store = MediaStore(object_store, url_ttl=getattr(settings, "media_url_ttl_s", 3600))
    retention = RetentionPolicy(
        derived_retention_days=getattr(settings, "media_derived_retention_days", 30)
    )
    return MediaService(
        store,
        session_factory=session_factory,
        retention=retention,
        url_ttl_s=getattr(settings, "media_url_ttl_s", 3600),
        segment_s=getattr(settings, "media_segment_s", 4),
        sprite_count=getattr(settings, "media_sprite_count", 20),
        gc_batch=getattr(settings, "media_gc_batch", 100),
    )


__all__ = ["MediaService", "build_media_service"]
