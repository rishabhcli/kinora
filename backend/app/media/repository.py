"""Repository for the ``media_assets`` registry.

Holds the queries for the per-asset inventory: upsert-by-key (the registration
path the service uses after a content-addressed put), dedup lookup by content
hash, reference counting (so GC only collects truly-orphaned derivatives), and
the retention sweep selector. Follows the project convention: flush, never
commit — the unit-of-work boundary owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.base import new_id
from app.db.repositories.base import BaseRepository
from app.media.kinds import MediaAssetKind
from app.media.metadata import AssetMetadata
from app.media.models import MediaAsset


class MediaAssetRepo(BaseRepository):
    """Create, query, and lifecycle-manage media-asset rows."""

    async def get(self, asset_id: str) -> MediaAsset | None:
        """Fetch an asset by id."""
        return await self.session.get(MediaAsset, asset_id)

    async def get_by_key(self, storage_key: str) -> MediaAsset | None:
        """Fetch the asset registered at ``storage_key`` (keys are unique-ish)."""
        stmt = select(MediaAsset).where(MediaAsset.storage_key == storage_key).limit(1)
        return (await self.session.execute(stmt)).scalars().first()

    async def find_by_hash(
        self, content_hash: str, *, kind: MediaAssetKind | None = None
    ) -> MediaAsset | None:
        """Find an existing asset by content hash (the §8.7 dedup probe)."""
        stmt = select(MediaAsset).where(MediaAsset.content_hash == content_hash)
        if kind is not None:
            stmt = stmt.where(MediaAsset.kind == kind)
        stmt = stmt.order_by(MediaAsset.created_at).limit(1)
        return (await self.session.execute(stmt)).scalars().first()

    async def create(
        self,
        *,
        storage_key: str,
        kind: MediaAssetKind = MediaAssetKind.OTHER,
        content_hash: str | None = None,
        content_type: str = "application/octet-stream",
        size_bytes: int = 0,
        width: int | None = None,
        height: int | None = None,
        duration_s: float | None = None,
        etag: str | None = None,
        book_id: str | None = None,
        meta: dict[str, Any] | None = None,
        ref_count: int = 0,
        expires_at: datetime | None = None,
        asset_id: str | None = None,
    ) -> MediaAsset:
        """Insert a media-asset row."""
        asset = MediaAsset(
            id=asset_id or new_id(),
            storage_key=storage_key,
            kind=kind,
            content_hash=content_hash,
            content_type=content_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            duration_s=duration_s,
            etag=etag,
            book_id=book_id,
            meta=meta,
            ref_count=ref_count,
            expires_at=expires_at,
        )
        self.session.add(asset)
        await self.session.flush()
        return asset

    async def register(
        self,
        meta: AssetMetadata,
        *,
        ref_count: int = 0,
        expires_at: datetime | None = None,
        extra_meta: dict[str, Any] | None = None,
    ) -> MediaAsset:
        """Idempotently register an :class:`AssetMetadata` (upsert by key).

        If a row already exists for the key, its facts are refreshed (size,
        hash, geometry, content-type, merged ``meta``); otherwise a new row is
        inserted. Returns the live row either way — the registration path the
        service calls after a content-addressed put.
        """
        merged_meta = {**(meta.meta or {}), **(extra_meta or {})}
        existing = await self.get_by_key(meta.storage_key)
        if existing is not None:
            existing.kind = meta.kind
            existing.content_hash = meta.content_hash
            existing.content_type = meta.content_type
            existing.size_bytes = meta.size_bytes
            existing.width = meta.width
            existing.height = meta.height
            existing.duration_s = meta.duration_s
            existing.etag = meta.etag
            existing.book_id = meta.book_id
            existing.meta = merged_meta or None
            if expires_at is not None:
                existing.expires_at = expires_at
            await self.session.flush()
            return existing
        return await self.create(
            storage_key=meta.storage_key,
            kind=meta.kind,
            content_hash=meta.content_hash,
            content_type=meta.content_type,
            size_bytes=meta.size_bytes,
            width=meta.width,
            height=meta.height,
            duration_s=meta.duration_s,
            etag=meta.etag,
            book_id=meta.book_id,
            meta=merged_meta or None,
            ref_count=ref_count,
            expires_at=expires_at,
        )

    async def incr_ref(self, asset_id: str, *, by: int = 1) -> MediaAsset | None:
        """Adjust an asset's reference count (clamped at zero)."""
        asset = await self.session.get(MediaAsset, asset_id)
        if asset is None:
            return None
        asset.ref_count = max(0, asset.ref_count + by)
        await self.session.flush()
        return asset

    async def set_expiry(self, asset_id: str, expires_at: datetime | None) -> MediaAsset | None:
        """Set/clear the retention horizon for an asset."""
        asset = await self.session.get(MediaAsset, asset_id)
        if asset is None:
            return None
        asset.expires_at = expires_at
        await self.session.flush()
        return asset

    async def list_for_book(
        self, book_id: str, *, kind: MediaAssetKind | None = None
    ) -> list[MediaAsset]:
        """All assets for a book (optionally one kind), newest first."""
        stmt = select(MediaAsset).where(MediaAsset.book_id == book_id)
        if kind is not None:
            stmt = stmt.where(MediaAsset.kind == kind)
        stmt = stmt.order_by(MediaAsset.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_expired(
        self, *, now: datetime, kinds: Sequence[MediaAssetKind] | None = None, limit: int = 100
    ) -> list[MediaAsset]:
        """Assets past their retention horizon with no live references.

        The GC selector: ``expires_at <= now`` AND ``ref_count == 0``. Restricting
        to ``kinds`` (e.g. the derived set) keeps the sweep from touching primary
        assets governed by explicit retention.
        """
        stmt = select(MediaAsset).where(
            MediaAsset.expires_at.is_not(None),
            MediaAsset.expires_at <= now,
            MediaAsset.ref_count == 0,
        )
        if kinds is not None:
            stmt = stmt.where(MediaAsset.kind.in_(list(kinds)))
        stmt = stmt.order_by(MediaAsset.expires_at).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete(self, asset_id: str) -> bool:
        """Delete an asset row; returns whether a row was removed."""
        asset = await self.session.get(MediaAsset, asset_id)
        if asset is None:
            return False
        await self.session.delete(asset)
        await self.session.flush()
        return True

    async def total_bytes(self, *, book_id: str | None = None) -> int:
        """Sum of ``size_bytes`` across assets (optionally one book)."""
        from sqlalchemy import func

        stmt = select(func.coalesce(func.sum(MediaAsset.size_bytes), 0))
        if book_id is not None:
            stmt = stmt.where(MediaAsset.book_id == book_id)
        return int((await self.session.execute(stmt)).scalar_one())


__all__ = ["MediaAssetRepo"]
