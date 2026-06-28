"""Integration tests for the /api/media router (skip without throwaway infra).

The media router is in the shared ROUTERS list, so the standard ``api_client``
fixture mounts it. These exercise ownership scoping, the registry projection,
and signed-URL minting end-to-end against the throwaway Postgres + MinIO.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.composition import Container
from app.media.kinds import MediaAssetKind
from app.media.repository import MediaAssetRepo
from tests.conftest import register_login, requires_infra, seed_owned_book

pytestmark = requires_infra


async def _seed_asset(
    container: Container, book_id: str, *, kind: MediaAssetKind, key: str, size: int = 10
) -> str:
    async with container.session_factory() as session:
        asset = await MediaAssetRepo(session).create(
            storage_key=key,
            kind=kind,
            content_type="video/mp4" if kind == MediaAssetKind.CLIP else "image/png",
            size_bytes=size,
            book_id=book_id,
        )
        return asset.id


async def test_list_book_assets(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers, title="Reel")
    await _seed_asset(container, book_id, kind=MediaAssetKind.CLIP, key="clips/b/s.mp4")
    await _seed_asset(container, book_id, kind=MediaAssetKind.POSTER, key="posters/b/p.png")

    resp = await api_client.get(f"/api/media/books/{book_id}/assets", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["book_id"] == book_id
    assert len(body["assets"]) == 2
    kinds = {a["kind"] for a in body["assets"]}
    assert kinds == {"clip", "poster"}
    # every asset carries a browser-reachable URL
    assert all(a["url"] for a in body["assets"])


async def test_list_filtered_by_kind(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    await _seed_asset(container, book_id, kind=MediaAssetKind.CLIP, key="c.mp4")
    await _seed_asset(container, book_id, kind=MediaAssetKind.POSTER, key="p.png")

    resp = await api_client.get(
        f"/api/media/books/{book_id}/assets?kind=poster", headers=auth_headers
    )
    assert resp.status_code == 200
    assets = resp.json()["assets"]
    assert len(assets) == 1
    assert assets[0]["kind"] == "poster"


async def test_get_asset(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    asset_id = await _seed_asset(
        container, book_id, kind=MediaAssetKind.CLIP, key="clips/x.mp4", size=42
    )
    resp = await api_client.get(f"/api/media/assets/{asset_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == asset_id
    assert body["size_bytes"] == 42
    assert body["url"]


async def test_mint_url_clamps_ttl(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    asset_id = await _seed_asset(container, book_id, kind=MediaAssetKind.CLIP, key="m.mp4")
    resp = await api_client.post(
        f"/api/media/assets/{asset_id}/url?ttl_s=5", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asset_id"] == asset_id
    assert body["ttl_s"] == 60  # clamped up to MIN_TTL_S
    assert body["url"]


async def test_other_user_cannot_read_assets(
    api_client: AsyncClient, container: Container, auth_headers: dict[str, str]
) -> None:
    book_id = await seed_owned_book(api_client, container, auth_headers)
    asset_id = await _seed_asset(container, book_id, kind=MediaAssetKind.CLIP, key="o.mp4")
    other = await register_login(api_client, "intruder@example.com")

    # listing another user's book → 404
    resp = await api_client.get(f"/api/media/books/{book_id}/assets", headers=other)
    assert resp.status_code == 404
    # fetching another user's asset → 404
    resp = await api_client.get(f"/api/media/assets/{asset_id}", headers=other)
    assert resp.status_code == 404


async def test_missing_asset_404(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await api_client.get("/api/media/assets/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


async def test_requires_auth(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/media/books/whatever/assets")
    assert resp.status_code in (401, 403)
