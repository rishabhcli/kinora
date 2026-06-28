"""API-level integration tests for the workspaces router (infra-gated).

Drives the HTTP surface end-to-end through the wired test container: workspace
CRUD, members + invitation accept, sharing, the ``can`` access probe, transfer of
ownership, seats, and the activity feed. Skips cleanly with no test infra.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import AsyncClient

from app.composition import Container
from app.db.base import new_id
from app.db.repositories.book import BookRepo

from .conftest import register_login, requires_infra, user_id_for

pytestmark = requires_infra


@pytest_asyncio.fixture
async def owner_headers(api_client: AsyncClient) -> dict[str, str]:
    return await register_login(api_client, "api-ws-owner@example.com")


async def _seed_book(container: Container, owner: str, title: str = "API Tale") -> str:
    book_id = new_id()
    async with container.session_factory() as session:
        await BookRepo(session).create(title=title, book_id=book_id, user_id=owner)
    return book_id


async def test_create_and_list_workspace(api_client: AsyncClient, owner_headers: dict) -> None:
    resp = await api_client.post(
        "/api/workspaces", json={"name": "Studio One"}, headers=owner_headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Studio One"
    assert body["my_role"] == "owner"

    listing = await api_client.get("/api/workspaces", headers=owner_headers)
    assert listing.status_code == 200
    assert any(w["id"] == body["id"] for w in listing.json())


async def test_unauthenticated_rejected(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/workspaces")
    assert resp.status_code == 401


async def test_get_workspace_requires_access(
    api_client: AsyncClient, owner_headers: dict
) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Private WS"}, headers=owner_headers
    )
    ws_id = create.json()["id"]
    stranger = await register_login(api_client, "api-stranger@example.com")
    resp = await api_client.get(f"/api/workspaces/{ws_id}", headers=stranger)
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "forbidden"


async def test_invite_and_accept_flow(api_client: AsyncClient, owner_headers: dict) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Invite WS"}, headers=owner_headers
    )
    ws_id = create.json()["id"]

    # Register the invitee so their email exists for the email-match check.
    invitee_headers = await register_login(api_client, "api-invitee@example.com")

    invite = await api_client.post(
        f"/api/workspaces/{ws_id}/invitations",
        json={"email": "api-invitee@example.com", "role": "editor"},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]
    assert token

    accept = await api_client.post(
        "/api/workspaces/invitations/accept",
        json={"token": token},
        headers=invitee_headers,
    )
    assert accept.status_code == 200, accept.text
    assert accept.json()["role"] == "editor"

    # The invitee can now view the workspace and edit it.
    invitee_id = await user_id_for(api_client, invitee_headers)
    probe = await api_client.get(
        f"/api/workspaces/access/workspace/{ws_id}/edit", headers=invitee_headers
    )
    assert probe.status_code == 200
    assert probe.json()["allowed"] is True
    assert invitee_id  # sanity


async def test_member_management(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Managed WS"}, headers=owner_headers
    )
    ws_id = create.json()["id"]
    member_headers = await register_login(api_client, "api-member@example.com")
    member_id = await user_id_for(api_client, member_headers)

    add = await api_client.post(
        f"/api/workspaces/{ws_id}/members",
        json={"user_id": member_id, "role": "viewer"},
        headers=owner_headers,
    )
    assert add.status_code == 201, add.text

    members = await api_client.get(f"/api/workspaces/{ws_id}/members", headers=owner_headers)
    assert members.status_code == 200
    assert any(m["user_id"] == member_id for m in members.json())

    promote = await api_client.patch(
        f"/api/workspaces/{ws_id}/members/{member_id}",
        json={"role": "editor"},
        headers=owner_headers,
    )
    assert promote.status_code == 200
    assert promote.json()["role"] == "editor"

    remove = await api_client.delete(
        f"/api/workspaces/{ws_id}/members/{member_id}", headers=owner_headers
    )
    assert remove.status_code == 200


async def test_share_book_and_probe(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    grantee_headers = await register_login(api_client, "api-grantee@example.com")
    grantee_id = await user_id_for(api_client, grantee_headers)

    share = await api_client.post(
        f"/api/workspaces/books/{book_id}/shares",
        json={"user_id": grantee_id, "role": "commenter"},
        headers=owner_headers,
    )
    assert share.status_code == 200, share.text

    # Grantee can comment but not edit.
    can_comment = await api_client.get(
        f"/api/workspaces/access/book/{book_id}/comment", headers=grantee_headers
    )
    assert can_comment.json()["allowed"] is True
    can_edit = await api_client.get(
        f"/api/workspaces/access/book/{book_id}/edit", headers=grantee_headers
    )
    assert can_edit.json()["allowed"] is False

    # Access summary reflects the commenter capabilities.
    summary = await api_client.get(
        f"/api/workspaces/access/book/{book_id}", headers=grantee_headers
    )
    assert summary.status_code == 200
    assert summary.json()["effective_role"] == "commenter"
    assert "comment" in summary.json()["allowed_actions"]


async def test_share_with_unknown_email_404(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    resp = await api_client.post(
        f"/api/workspaces/books/{book_id}/shares",
        json={"email": "nobody-here@example.com", "role": "viewer"},
        headers=owner_headers,
    )
    assert resp.status_code == 404


async def test_non_owner_cannot_share_book(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    stranger = await register_login(api_client, "api-thief@example.com")
    stranger_id = await user_id_for(api_client, stranger)
    resp = await api_client.post(
        f"/api/workspaces/books/{book_id}/shares",
        json={"user_id": stranger_id, "role": "viewer"},
        headers=stranger,
    )
    assert resp.status_code == 403


async def test_transfer_ownership_flow(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    recipient = await register_login(api_client, "api-newowner@example.com")
    recipient_id = await user_id_for(api_client, recipient)

    req = await api_client.post(
        f"/api/workspaces/books/{book_id}/transfer",
        json={"to_user_id": recipient_id},
        headers=owner_headers,
    )
    assert req.status_code == 200, req.text
    transfer_id = req.json()["id"]

    respond = await api_client.post(
        f"/api/workspaces/transfers/{transfer_id}/respond",
        json={"accept": True},
        headers=recipient,
    )
    assert respond.status_code == 200
    assert respond.json()["status"] == "accepted"

    # The recipient now owns the book.
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
        assert book is not None and book.user_id == recipient_id


async def test_settings_quota_and_seats(
    api_client: AsyncClient, owner_headers: dict
) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Quota WS"}, headers=owner_headers
    )
    body = create.json()
    ws_id = body["id"]
    org_id = body["org_id"]

    patch = await api_client.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"settings": {"max_books": 5, "default_member_role": "commenter"}},
        headers=owner_headers,
    )
    assert patch.status_code == 200
    assert patch.json()["settings"]["max_books"] == 5

    seats = await api_client.get(f"/api/workspaces/orgs/{org_id}/seats", headers=owner_headers)
    assert seats.status_code == 200
    assert seats.json()["active_members"] >= 1

    set_seats = await api_client.patch(
        f"/api/workspaces/orgs/{org_id}/seats", json={"seats": 10}, headers=owner_headers
    )
    assert set_seats.status_code == 200
    assert set_seats.json()["seats"] == 10


async def test_activity_feed_endpoint(api_client: AsyncClient, owner_headers: dict) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Feed WS"}, headers=owner_headers
    )
    ws_id = create.json()["id"]
    feed = await api_client.get(f"/api/workspaces/{ws_id}/activity", headers=owner_headers)
    assert feed.status_code == 200
    verbs = {row["verb"] for row in feed.json()}
    assert "workspace.created" in verbs


async def test_collection_endpoints(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    create = await api_client.post(
        "/api/workspaces", json={"name": "Coll WS"}, headers=owner_headers
    )
    ws_id = create.json()["id"]
    coll = await api_client.post(
        f"/api/workspaces/{ws_id}/collections",
        json={"name": "Shelf A"},
        headers=owner_headers,
    )
    assert coll.status_code == 201, coll.text
    coll_id = coll.json()["id"]
    add = await api_client.post(
        f"/api/workspaces/collections/{coll_id}/items",
        json={"book_id": book_id},
        headers=owner_headers,
    )
    assert add.status_code == 200
    remove = await api_client.delete(
        f"/api/workspaces/collections/{coll_id}/items/{book_id}", headers=owner_headers
    )
    assert remove.status_code == 200


async def test_generic_share_and_list_and_revoke(
    api_client: AsyncClient, owner_headers: dict, container: Container
) -> None:
    owner_id = await user_id_for(api_client, owner_headers)
    book_id = await _seed_book(container, owner_id)
    grantee_headers = await register_login(api_client, "api-generic-grantee@example.com")
    grantee_id = await user_id_for(api_client, grantee_headers)

    share = await api_client.post(
        f"/api/workspaces/shares/book/{book_id}",
        json={"user_id": grantee_id, "role": "editor"},
        headers=owner_headers,
    )
    assert share.status_code == 200, share.text

    listing = await api_client.get(
        f"/api/workspaces/shares/book/{book_id}", headers=owner_headers
    )
    assert listing.status_code == 200
    assert any(s["user_id"] == grantee_id for s in listing.json())

    revoke = await api_client.delete(
        f"/api/workspaces/shares/book/{book_id}/{grantee_id}", headers=owner_headers
    )
    assert revoke.status_code == 200

    # After revoke the grantee has no access.
    probe = await api_client.get(
        f"/api/workspaces/access/book/{book_id}", headers=grantee_headers
    )
    assert probe.json()["effective_role"] is None


async def test_delete_workspace(api_client: AsyncClient, owner_headers: dict) -> None:
    create = await api_client.post(
        "/api/workspaces", json={"name": "Doomed"}, headers=owner_headers
    )
    ws_id = create.json()["id"]
    delete = await api_client.delete(f"/api/workspaces/{ws_id}", headers=owner_headers)
    assert delete.status_code == 200
    # Now gone.
    fetch = await api_client.get(f"/api/workspaces/{ws_id}", headers=owner_headers)
    assert fetch.status_code == 404
