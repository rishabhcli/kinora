"""Media API — the media-asset registry over HTTP (Media domain; §8.7, §9, §12).

Read + mint routes over the :mod:`app.media` subsystem, scoped to the
authenticated owner's books:

* ``GET  /api/media/books/{book_id}/assets`` — every managed asset for a book
  (optionally filtered by ``kind``), each with a browser-reachable URL.
* ``GET  /api/media/assets/{asset_id}`` — one asset's metadata + URL.
* ``POST /api/media/assets/{asset_id}/url`` — mint a fresh (optionally
  short-lived) signed/CDN URL for an asset.

These are projections over the ``media_assets`` table populated by
:class:`app.media.service.MediaService`; they never render and work with
``KINORA_LIVE_VIDEO`` off. Ownership is enforced via the asset's ``book_id``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.media.kinds import MediaAssetKind
from app.media.models import MediaAsset
from app.media.repository import MediaAssetRepo

router = APIRouter(prefix="/media", tags=["media"])

#: Default media-URL lifetime (seconds); clamped at use to 60s..7d.
URL_TTL_S = 3600


class MediaAssetResponse(BaseModel):
    """Wire shape for one managed media asset."""

    model_config = ConfigDict(extra="forbid")

    id: str
    book_id: str | None
    kind: MediaAssetKind
    storage_key: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    url: str
    meta: dict[str, Any] = Field(default_factory=dict)


class MediaAssetsResponse(BaseModel):
    """A book's media assets."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    url_ttl_s: int
    assets: list[MediaAssetResponse]


class SignedUrlResponse(BaseModel):
    """A freshly-minted browser-reachable URL for an asset."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    url: str
    ttl_s: int


async def _assert_owns_book(session: AsyncSession, user: User, book_id: str) -> None:
    book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book", status=404)


async def _project(
    container: ContainerDep, asset: MediaAsset, *, ttl: int
) -> MediaAssetResponse:
    url = await container.media_service.url_for(asset.storage_key, ttl=ttl)
    return MediaAssetResponse(
        id=asset.id,
        book_id=asset.book_id,
        kind=asset.kind,
        storage_key=asset.storage_key,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        width=asset.width,
        height=asset.height,
        duration_s=asset.duration_s,
        url=url,
        meta=asset.meta or {},
    )


@router.get("/books/{book_id}/assets", response_model=MediaAssetsResponse)
async def list_book_assets(
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    kind: MediaAssetKind | None = None,
) -> MediaAssetsResponse:
    """Every managed media asset for a book (optionally one kind)."""
    async with container.session_factory() as session:
        await _assert_owns_book(session, user, book_id)
        rows = await MediaAssetRepo(session).list_for_book(book_id, kind=kind)
    assets = [await _project(container, row, ttl=URL_TTL_S) for row in rows]
    return MediaAssetsResponse(book_id=book_id, url_ttl_s=URL_TTL_S, assets=assets)


@router.get("/assets/{asset_id}", response_model=MediaAssetResponse)
async def get_asset(
    asset_id: str, container: ContainerDep, user: CurrentUser
) -> MediaAssetResponse:
    """One asset's metadata + a browser-reachable URL."""
    async with container.session_factory() as session:
        asset = await MediaAssetRepo(session).get(asset_id)
        if asset is None:
            raise APIError("asset_not_found", "no such media asset", status=404)
        # An asset scoped to a book must be owned; book-less assets are not exposed.
        if asset.book_id is None:
            raise APIError("asset_not_found", "no such media asset", status=404)
        await _assert_owns_book(session, user, asset.book_id)
    return await _project(container, asset, ttl=URL_TTL_S)


@router.post("/assets/{asset_id}/url", response_model=SignedUrlResponse)
async def mint_url(
    asset_id: str,
    container: ContainerDep,
    user: CurrentUser,
    ttl_s: int = URL_TTL_S,
) -> SignedUrlResponse:
    """Mint a fresh signed/CDN URL for an asset (e.g. a short-lived share)."""
    async with container.session_factory() as session:
        asset = await MediaAssetRepo(session).get(asset_id)
        if asset is None or asset.book_id is None:
            raise APIError("asset_not_found", "no such media asset", status=404)
        await _assert_owns_book(session, user, asset.book_id)
        key = asset.storage_key
    url = await container.media_service.url_for(key, ttl=ttl_s)
    from app.media.urls import clamp_ttl

    return SignedUrlResponse(asset_id=asset_id, url=url, ttl_s=clamp_ttl(ttl_s))
