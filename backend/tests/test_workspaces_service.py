"""Integration tests for the WorkspaceService + AuthorizationService (infra-gated).

These exercise the DB-backed resolution + decision path end-to-end against the
throwaway Postgres: workspace creation, membership, the email-token accept flow,
shares layered on the personal book owner, collections, transfer-of-ownership,
seat quotas, and the activity feed. They skip cleanly when no test DB is set.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.composition import Container
from app.db.base import new_id
from app.db.repositories.book import BookRepo
from app.db.repositories.user import UserRepo
from app.workspaces.authz import AuthorizationError, AuthorizationService, ResourceRef
from app.workspaces.roles import Action, MemberStatus, OrgPlan, ResourceType, Role
from app.workspaces.service import WorkspaceError, WorkspaceService

from .conftest import register_login, requires_infra, user_id_for

pytestmark = requires_infra

SECRET = "kinora-test-jwt-secret-key-which-is-comfortably-32-bytes"


@pytest_asyncio.fixture
async def owner_id(api_client: AsyncClient) -> str:
    headers = await register_login(api_client, "ws-owner@example.com")
    return await user_id_for(api_client, headers)


async def _make_user(container: Container, email: str) -> str:
    """Create a user row directly (no HTTP) and return its id."""
    async with container.session_factory() as session:
        user = await UserRepo(session).create(email=email.lower(), hashed_password="x")
    return user.id


async def _seed_book(container: Container, owner: str | None, title: str = "Tale") -> str:
    book_id = new_id()
    async with container.session_factory() as session:
        await BookRepo(session).create(title=title, book_id=book_id, user_id=owner)
    return book_id


def _svc(session) -> WorkspaceService:  # type: ignore[no-untyped-def]
    return WorkspaceService(session, invite_secret=SECRET)


# --------------------------------------------------------------------------- #
# Workspace + membership lifecycle
# --------------------------------------------------------------------------- #


async def test_create_workspace_makes_owner_member(container: Container, owner_id: str) -> None:
    async with container.session_factory() as session:
        ws = await _svc(session).create_workspace(owner_user_id=owner_id, name="My Team")
        assert ws.slug == "my-team"
        authz = AuthorizationService(session)
        assert await authz.effective_role(owner_id, ResourceRef.workspace(ws.id)) == Role.OWNER
        assert await authz.can(owner_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(ws.id))
        assert await authz.can(owner_id, Action.DELETE, ResourceRef.workspace(ws.id))


async def test_slug_collision_disambiguated(container: Container, owner_id: str) -> None:
    async with container.session_factory() as session:
        svc = _svc(session)
        org = await svc.create_organization(owner_user_id=owner_id, name="Org")
        ws1 = await svc.create_workspace(owner_user_id=owner_id, name="Studio", org_id=org.id)
        ws2 = await svc.create_workspace(owner_user_id=owner_id, name="Studio", org_id=org.id)
        assert ws1.slug == "studio"
        assert ws2.slug == "studio-2"


async def test_non_member_has_no_access(container: Container, owner_id: str) -> None:
    stranger = await _make_user(container, "stranger@example.com")
    async with container.session_factory() as session:
        ws = await _svc(session).create_workspace(owner_user_id=owner_id, name="Private")
        authz = AuthorizationService(session)
        assert await authz.effective_role(stranger, ResourceRef.workspace(ws.id)) is None
        assert not await authz.can(stranger, Action.VIEW, ResourceRef.workspace(ws.id))


async def test_add_member_grants_role(container: Container, owner_id: str) -> None:
    editor = await _make_user(container, "editor@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Crew")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=editor, role=Role.EDITOR
        )
        authz = AuthorizationService(session)
        assert await authz.can(editor, Action.EDIT, ResourceRef.workspace(ws.id))
        assert not await authz.can(editor, Action.MANAGE_MEMBERS, ResourceRef.workspace(ws.id))


async def test_member_cannot_manage_members(container: Container, owner_id: str) -> None:
    viewer = await _make_user(container, "viewer@example.com")
    target = await _make_user(container, "target@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Crew2")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=viewer, role=Role.VIEWER
        )
        with pytest.raises(AuthorizationError):
            await svc.add_member(
                actor_user_id=viewer, workspace_id=ws.id, user_id=target, role=Role.VIEWER
            )


async def test_remove_member_revokes_access(container: Container, owner_id: str) -> None:
    member = await _make_user(container, "leaving@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Crew3")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=member, role=Role.EDITOR
        )
        await svc.remove_member(actor_user_id=owner_id, workspace_id=ws.id, user_id=member)
        authz = AuthorizationService(session)
        assert await authz.effective_role(member, ResourceRef.workspace(ws.id)) is None


async def test_cannot_remove_org_owner(container: Container, owner_id: str) -> None:
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Solo")
        with pytest.raises(WorkspaceError, match="owner"):
            await svc.remove_member(
                actor_user_id=owner_id, workspace_id=ws.id, user_id=owner_id
            )


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #


async def test_invite_and_accept(container: Container, owner_id: str) -> None:
    invitee = await _make_user(container, "joiner@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Invited")
        result = await svc.invite_member(
            actor_user_id=owner_id,
            workspace_id=ws.id,
            email="joiner@example.com",
            role=Role.COMMENTER,
        )
        token = result.token
    async with container.session_factory() as session:
        svc = _svc(session)
        member = await svc.accept_invitation(token=token, accepting_user_id=invitee)
        assert member.role == Role.COMMENTER
        assert member.status == MemberStatus.ACTIVE
        authz = AuthorizationService(session)
        assert await authz.can(invitee, Action.COMMENT, ResourceRef.workspace(ws.id))


async def test_accept_wrong_email_rejected(container: Container, owner_id: str) -> None:
    wrong = await _make_user(container, "someone-else@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="W")
        result = await svc.invite_member(
            actor_user_id=owner_id, workspace_id=ws.id, email="intended@example.com"
        )
        token = result.token
    async with container.session_factory() as session:
        with pytest.raises(WorkspaceError, match="email"):
            await _svc(session).accept_invitation(token=token, accepting_user_id=wrong)


async def test_revoked_invitation_cannot_be_accepted(container: Container, owner_id: str) -> None:
    invitee = await _make_user(container, "revoked-invitee@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="W")
        result = await svc.invite_member(
            actor_user_id=owner_id, workspace_id=ws.id, email="revoked-invitee@example.com"
        )
        await svc.revoke_invitation(
            actor_user_id=owner_id, invitation_id=result.invitation.id
        )
        token = result.token
    async with container.session_factory() as session:
        with pytest.raises(WorkspaceError) as ei:
            await _svc(session).accept_invitation(token=token, accepting_user_id=invitee)
        assert ei.value.code == "invitation_not_pending"
        assert "revoked" in ei.value.message


# --------------------------------------------------------------------------- #
# Shares layered on the personal book owner (additive ownership)
# --------------------------------------------------------------------------- #


async def test_personal_owner_is_book_owner(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    async with container.session_factory() as session:
        authz = AuthorizationService(session)
        ref = ResourceRef.book(book_id)
        assert await authz.effective_role(owner_id, ref) == Role.OWNER
        assert await authz.can(owner_id, Action.SHARE, ref)
        assert await authz.can(owner_id, Action.TRANSFER_OWNERSHIP, ref)


async def test_direct_book_share(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    grantee = await _make_user(container, "grantee@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        await svc.share_resource(
            actor_user_id=owner_id,
            resource=ResourceRef.book(book_id),
            grantee_user_id=grantee,
            role=Role.COMMENTER,
        )
        authz = AuthorizationService(session)
        ref = ResourceRef.book(book_id)
        assert await authz.can(grantee, Action.COMMENT, ref)
        assert not await authz.can(grantee, Action.EDIT, ref)
        # The personal owner is unaffected (additive).
        assert await authz.effective_role(owner_id, ref) == Role.OWNER


async def test_non_owner_cannot_share(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    stranger = await _make_user(container, "nosy@example.com")
    third = await _make_user(container, "third@example.com")
    async with container.session_factory() as session:
        with pytest.raises(AuthorizationError):
            await _svc(session).share_resource(
                actor_user_id=stranger,
                resource=ResourceRef.book(book_id),
                grantee_user_id=third,
                role=Role.VIEWER,
            )


async def test_workspace_book_grants_inherited_role(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    member = await _make_user(container, "shelf-member@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Shelf")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=member, role=Role.EDITOR
        )
        await svc.attach_book(actor_user_id=owner_id, workspace_id=ws.id, book_id=book_id)
        authz = AuthorizationService(session)
        ref = ResourceRef.book(book_id)
        # The editor member reaches the book through the workspace shelf.
        assert await authz.can(member, Action.EDIT, ref)
        assert await authz.can(member, Action.RENDER, ref)
        assert not await authz.can(member, Action.SHARE, ref)


async def test_revoke_share(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    grantee = await _make_user(container, "temp@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ref = ResourceRef.book(book_id)
        await svc.share_resource(
            actor_user_id=owner_id, resource=ref, grantee_user_id=grantee, role=Role.VIEWER
        )
        await svc.revoke_share(
            actor_user_id=owner_id, resource=ref, grantee_user_id=grantee
        )
        authz = AuthorizationService(session)
        assert await authz.effective_role(grantee, ref) is None


async def test_accessible_books_aggregates_paths(container: Container, owner_id: str) -> None:
    shared_book = await _seed_book(container, owner_id, title="Shared")
    shelf_book = await _seed_book(container, owner_id, title="Shelf")
    member = await _make_user(container, "aggregator@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Agg")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=member, role=Role.VIEWER
        )
        await svc.attach_book(actor_user_id=owner_id, workspace_id=ws.id, book_id=shelf_book)
        await svc.share_resource(
            actor_user_id=owner_id,
            resource=ResourceRef.book(shared_book),
            grantee_user_id=member,
            role=Role.VIEWER,
        )
        accessible = await svc.list_shared_books_for_user(member)
        assert set(accessible) == {shared_book, shelf_book}


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


async def test_collection_lifecycle(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Coll WS")
        coll = await svc.create_collection(
            actor_user_id=owner_id, workspace_id=ws.id, name="Favorites"
        )
        await svc.add_to_collection(
            actor_user_id=owner_id, collection_id=coll.id, book_id=book_id
        )
        items = await svc.repos.collections.list_items(coll.id)
        assert [i.book_id for i in items] == [book_id]
        await svc.remove_from_collection(
            actor_user_id=owner_id, collection_id=coll.id, book_id=book_id
        )
        assert await svc.repos.collections.list_items(coll.id) == []


# --------------------------------------------------------------------------- #
# Transfer of ownership
# --------------------------------------------------------------------------- #


async def test_transfer_book_ownership(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    recipient = await _make_user(container, "new-owner@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        transfer = await svc.request_transfer(
            actor_user_id=owner_id,
            resource=ResourceRef.book(book_id),
            to_user_id=recipient,
        )
        await svc.respond_to_transfer(
            actor_user_id=recipient, transfer_id=transfer.id, accept=True
        )
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
        assert book is not None and book.user_id == recipient
        authz = AuthorizationService(session)
        assert await authz.effective_role(recipient, ResourceRef.book(book_id)) == Role.OWNER
        # Old owner no longer owns it.
        assert await authz.effective_role(owner_id, ResourceRef.book(book_id)) is None


async def test_transfer_decline_keeps_owner(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    recipient = await _make_user(container, "declining@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        transfer = await svc.request_transfer(
            actor_user_id=owner_id, resource=ResourceRef.book(book_id), to_user_id=recipient
        )
        await svc.respond_to_transfer(
            actor_user_id=recipient, transfer_id=transfer.id, accept=False
        )
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
        assert book is not None and book.user_id == owner_id


async def test_transfer_only_recipient_can_respond(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    recipient = await _make_user(container, "rcpt@example.com")
    interloper = await _make_user(container, "interloper@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        transfer = await svc.request_transfer(
            actor_user_id=owner_id, resource=ResourceRef.book(book_id), to_user_id=recipient
        )
        with pytest.raises(WorkspaceError, match="recipient"):
            await svc.respond_to_transfer(
                actor_user_id=interloper, transfer_id=transfer.id, accept=True
            )


# --------------------------------------------------------------------------- #
# Seats + quotas
# --------------------------------------------------------------------------- #


async def test_seat_quota_blocks_overfill(container: Container, owner_id: str) -> None:
    async with container.session_factory() as session:
        svc = _svc(session)
        org = await svc.create_organization(
            owner_user_id=owner_id, name="Tiny", plan=OrgPlan.FREE
        )
        await svc.set_seats(actor_user_id=owner_id, org_id=org.id, seats=2)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="WS", org_id=org.id)
        u2 = await _make_user(container, "seat2@example.com")
        u3 = await _make_user(container, "seat3@example.com")
        # Owner is seat 1; u2 fills seat 2; u3 is over.
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=u2, role=Role.VIEWER
        )
        from app.workspaces.quotas import QuotaExceeded

        with pytest.raises(QuotaExceeded):
            await svc.add_member(
                actor_user_id=owner_id, workspace_id=ws.id, user_id=u3, role=Role.VIEWER
            )


async def test_max_books_quota(container: Container, owner_id: str) -> None:
    from app.workspaces.quotas import QuotaExceeded

    b1 = await _seed_book(container, owner_id, title="B1")
    b2 = await _seed_book(container, owner_id, title="B2")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(
            owner_user_id=owner_id, name="Capped", settings={"max_books": 1}
        )
        await svc.attach_book(actor_user_id=owner_id, workspace_id=ws.id, book_id=b1)
        with pytest.raises(QuotaExceeded):
            await svc.attach_book(actor_user_id=owner_id, workspace_id=ws.id, book_id=b2)


# --------------------------------------------------------------------------- #
# Activity feed
# --------------------------------------------------------------------------- #


async def test_activity_feed_records_operations(container: Container, owner_id: str) -> None:
    member = await _make_user(container, "feed-member@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Loud")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=member, role=Role.EDITOR
        )
        feed = await svc.activity_feed(actor_user_id=owner_id, workspace_id=ws.id)
        verbs = {row.verb for row in feed}
        assert "workspace.created" in verbs
        assert "member.added" in verbs


async def test_activity_feed_requires_access(container: Container, owner_id: str) -> None:
    stranger = await _make_user(container, "feed-stranger@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Quiet")
        with pytest.raises(AuthorizationError):
            await svc.activity_feed(actor_user_id=stranger, workspace_id=ws.id)


async def test_org_owner_owns_all_workspaces(container: Container, owner_id: str) -> None:
    async with container.session_factory() as session:
        svc = _svc(session)
        org = await svc.create_organization(owner_user_id=owner_id, name="Big Org")
        # A workspace whose membership the owner is not explicitly added to still
        # resolves to OWNER via the org-owner path.
        ws = await svc.repos.workspaces.create(org_id=org.id, name="Orphan", slug="orphan")
        authz = AuthorizationService(session)
        assert await authz.effective_role(owner_id, ResourceRef.workspace(ws.id)) == Role.OWNER


async def test_resource_share_role_is_filtered(container: Container, owner_id: str) -> None:
    """A workspace share grants a collection-level role via the workspace path."""
    member = await _make_user(container, "coll-viewer@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ws = await svc.create_workspace(owner_user_id=owner_id, name="Coll Authz")
        await svc.add_member(
            actor_user_id=owner_id, workspace_id=ws.id, user_id=member, role=Role.COMMENTER
        )
        coll = await svc.create_collection(
            actor_user_id=owner_id, workspace_id=ws.id, name="C"
        )
        authz = AuthorizationService(session)
        ref = ResourceRef.collection(coll.id)
        assert await authz.can(member, Action.VIEW, ref)
        assert await authz.can(member, Action.COMMENT, ref)
        assert not await authz.can(member, Action.MANAGE_COLLECTIONS, ref)


async def test_list_resource_shares(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    g1 = await _make_user(container, "list-share-1@example.com")
    g2 = await _make_user(container, "list-share-2@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ref = ResourceRef.book(book_id)
        await svc.share_resource(
            actor_user_id=owner_id, resource=ref, grantee_user_id=g1, role=Role.VIEWER
        )
        await svc.share_resource(
            actor_user_id=owner_id, resource=ref, grantee_user_id=g2, role=Role.COMMENTER
        )
        shares = await svc.list_resource_shares(actor_user_id=owner_id, resource=ref)
        by_user = {s.user_id: s.role for s in shares}
        assert by_user == {g1: Role.VIEWER, g2: Role.COMMENTER}


async def test_share_with_self_rejected(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    async with container.session_factory() as session:
        with pytest.raises(WorkspaceError, match="self"):
            await _svc(session).share_resource(
                actor_user_id=owner_id,
                resource=ResourceRef.book(book_id),
                grantee_user_id=owner_id,
                role=Role.EDITOR,
            )


async def test_share_upsert_changes_role(container: Container, owner_id: str) -> None:
    book_id = await _seed_book(container, owner_id)
    grantee = await _make_user(container, "upsert@example.com")
    async with container.session_factory() as session:
        svc = _svc(session)
        ref = ResourceRef.book(book_id)
        await svc.share_resource(
            actor_user_id=owner_id, resource=ref, grantee_user_id=grantee, role=Role.VIEWER
        )
        # Re-share at a higher role: upsert, not a duplicate row.
        await svc.share_resource(
            actor_user_id=owner_id, resource=ref, grantee_user_id=grantee, role=Role.EDITOR
        )
        shares = await svc.list_resource_shares(actor_user_id=owner_id, resource=ref)
        assert len(shares) == 1
        assert shares[0].role == Role.EDITOR


def test_resource_type_enum_round_trip() -> None:
    # A trivial guard so the module also has a no-infra-needed assertion.
    assert ResourceType("book") == ResourceType.BOOK
