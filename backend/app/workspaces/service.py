"""The workspace orchestration service.

The verb layer that ties the data layer (:mod:`app.workspaces.repositories`), the
authorization engine (:mod:`app.workspaces.authz`), the quota policy
(:mod:`app.workspaces.quotas`), and the invitation tokens
(:mod:`app.workspaces.invitations`) into the operations the API exposes:
create an org/workspace, manage members + email-token invitations, share books +
collections, transfer ownership, manage seats + settings, and read the activity
feed.

Every state-changing operation that the caller is not implicitly entitled to runs
through :class:`~app.workspaces.authz.AuthorizationService` first (raising
:class:`~app.workspaces.authz.AuthorizationError` on deny) and emits a
:class:`~app.workspaces.models.WorkspaceActivity` row, so the feed is a faithful
audit trail. Repositories ``flush``; the service is given an already-open session
whose transaction the *caller* (the request boundary) commits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.db.repositories.user import UserRepo
from app.workspaces.authz import AuthorizationService, ResourceRef
from app.workspaces.invitations import (
    DEFAULT_INVITE_TTL_S,
    create_invitation_token,
    new_invitation_id,
    verify_invitation_token,
)
from app.workspaces.models import (
    Collection,
    Organization,
    OwnershipTransfer,
    ResourceShare,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from app.workspaces.quotas import (
    SeatUsage,
    check_book_quota,
    check_seat_quota,
    default_member_role_for,
    default_seats_for_plan,
)
from app.workspaces.repositories import WorkspaceRepos
from app.workspaces.roles import (
    Action,
    InvitationStatus,
    MemberStatus,
    OrgPlan,
    ResourceType,
    Role,
    TransferStatus,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WorkspaceError(Exception):
    """A user-facing workspace operation error (mapped to a typed 4xx by the API)."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def slugify(text: str) -> str:
    """A URL-safe slug from a name (lower-cased, hyphenated, trimmed)."""
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "workspace"


@dataclass(frozen=True, slots=True)
class InvitationResult:
    """The result of issuing an invitation — the row + the token to email out."""

    invitation: WorkspaceInvitation
    token: str


class WorkspaceService:
    """Orchestrates org/workspace/membership/sharing/transfer operations.

    Bind one to an open :class:`AsyncSession`; the caller owns the commit. The
    ``invite_secret`` signs the email-token invitations (the app's ``jwt_secret``).
    """

    def __init__(self, session: AsyncSession, *, invite_secret: str) -> None:
        self.session = session
        self.repos = WorkspaceRepos(session)
        self.authz = AuthorizationService(session)
        self._invite_secret = invite_secret
        self._users = UserRepo(session)

    # ------------------------------------------------------------------ #
    # Organizations
    # ------------------------------------------------------------------ #

    async def create_organization(
        self, *, owner_user_id: str, name: str, plan: OrgPlan = OrgPlan.FREE
    ) -> Organization:
        """Create an org owned by ``owner_user_id`` with the plan's default seats."""
        org = await self.repos.orgs.create(
            name=name,
            owner_user_id=owner_user_id,
            plan=plan,
            seats=default_seats_for_plan(plan),
        )
        await self.repos.activity.record(
            verb="org.created", actor_user_id=owner_user_id, data={"org_id": org.id, "name": name}
        )
        return org

    async def set_seats(self, *, actor_user_id: str, org_id: str, seats: int) -> Organization:
        """Adjust an org's purchased seats (org owner only)."""
        org = await self._require_org_owner(actor_user_id, org_id)
        if seats < 0:
            raise WorkspaceError("invalid_seats", "seats must be >= 0")
        active = await self.repos.members.count_active_in_org(org_id)
        if seats != 0 and seats < active:
            raise WorkspaceError(
                "seats_below_usage",
                f"cannot set {seats} seats below {active} active members",
                status=409,
            )
        await self.repos.orgs.set_seats(org_id, seats)
        org.seats = seats
        await self.repos.activity.record(
            verb="org.seats_changed",
            actor_user_id=actor_user_id,
            data={"org_id": org_id, "seats": seats},
        )
        return org

    async def seat_usage(self, org_id: str) -> SeatUsage:
        """Current seat consumption for an org (the seat-management view)."""
        org = await self.repos.orgs.get(org_id)
        if org is None:
            raise WorkspaceError("org_not_found", "no such organization", status=404)
        active = await self.repos.members.count_active_in_org(org_id)
        return SeatUsage(seats=org.seats, active_members=active)

    # ------------------------------------------------------------------ #
    # Workspaces
    # ------------------------------------------------------------------ #

    async def create_workspace(
        self,
        *,
        owner_user_id: str,
        name: str,
        org_id: str | None = None,
        org_name: str | None = None,
        description: str | None = None,
        settings: dict | None = None,
    ) -> Workspace:
        """Create a workspace (creating a personal org first if none is given).

        The creator becomes an OWNER member. If no ``org_id`` is supplied a fresh
        org owned by the creator is minted, so the common single-team case needs
        only a name.
        """
        if org_id is None:
            new_org = await self.create_organization(
                owner_user_id=owner_user_id, name=org_name or f"{name} org"
            )
            org_id = new_org.id
        else:
            org = await self.repos.orgs.get(org_id)
            if org is None:
                raise WorkspaceError("org_not_found", "no such organization", status=404)
            # Only the org owner may create workspaces inside an existing org.
            if org.owner_user_id != owner_user_id:
                raise WorkspaceError(
                    "forbidden", "only the org owner can add workspaces", status=403
                )

        slug = await self._unique_workspace_slug(org_id, slugify(name))
        ws = await self.repos.workspaces.create(
            org_id=org_id,
            name=name,
            slug=slug,
            description=description,
            settings=settings or {},
        )
        await self.repos.members.upsert(
            workspace_id=ws.id, user_id=owner_user_id, role=Role.OWNER, status=MemberStatus.ACTIVE
        )
        await self.repos.activity.record(
            verb="workspace.created",
            workspace_id=ws.id,
            actor_user_id=owner_user_id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=ws.id,
            data={"name": name, "slug": slug},
        )
        return ws

    async def update_workspace_settings(
        self, *, actor_user_id: str, workspace_id: str, settings: dict
    ) -> Workspace:
        """Replace a workspace's settings/quotas bag (needs MANAGE_SETTINGS)."""
        ws = await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_SETTINGS, ResourceRef.workspace(workspace_id)
        )
        await self.repos.workspaces.update_settings(workspace_id, settings)
        ws.settings = settings
        await self.repos.activity.record(
            verb="workspace.settings_updated",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace_id,
        )
        return ws

    async def archive_workspace(
        self, *, actor_user_id: str, workspace_id: str, archived: bool = True
    ) -> Workspace:
        """Archive/unarchive a workspace (needs MANAGE_SETTINGS)."""
        ws = await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_SETTINGS, ResourceRef.workspace(workspace_id)
        )
        await self.repos.workspaces.update_fields(workspace_id, archived=archived)
        ws.archived = archived
        await self.repos.activity.record(
            verb="workspace.archived" if archived else "workspace.unarchived",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.WORKSPACE,
            resource_id=workspace_id,
        )
        return ws

    async def delete_workspace(self, *, actor_user_id: str, workspace_id: str) -> None:
        """Destroy a workspace and its edges (needs DELETE)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.DELETE, ResourceRef.workspace(workspace_id)
        )
        await self.repos.workspaces.delete(workspace_id)
        await self.repos.activity.record(
            verb="workspace.deleted",
            actor_user_id=actor_user_id,
            data={"workspace_id": workspace_id},
        )

    async def list_workspaces_for_user(self, user_id: str) -> list[Workspace]:
        """Every workspace a user can reach (membership + orgs they own)."""
        seen: dict[str, Workspace] = {}
        for member in await self.repos.members.list_for_user(user_id):
            ws = await self.repos.workspaces.get(member.workspace_id)
            if ws is not None:
                seen[ws.id] = ws
        for org in await self.repos.orgs.list_for_owner(user_id):
            for ws in await self.repos.workspaces.list_for_org(org.id):
                seen[ws.id] = ws
        return sorted(seen.values(), key=lambda w: w.created_at, reverse=True)

    # ------------------------------------------------------------------ #
    # Membership
    # ------------------------------------------------------------------ #

    async def add_member(
        self, *, actor_user_id: str, workspace_id: str, user_id: str, role: Role
    ) -> WorkspaceMember:
        """Directly add an existing user to a workspace (needs MANAGE_MEMBERS)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(workspace_id)
        )
        await self._guard_seat(workspace_id, adding_user_id=user_id)
        member = await self.repos.members.upsert(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            status=MemberStatus.ACTIVE,
            invited_by_user_id=actor_user_id,
        )
        await self.repos.activity.record(
            verb="member.added",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            data={"user_id": user_id, "role": role.value},
        )
        return member

    async def change_member_role(
        self, *, actor_user_id: str, workspace_id: str, user_id: str, role: Role
    ) -> WorkspaceMember:
        """Change a member's role (needs MANAGE_MEMBERS; cannot demote the org owner)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(workspace_id)
        )
        member = await self.repos.members.get(workspace_id, user_id)
        if member is None:
            raise WorkspaceError("member_not_found", "no such member", status=404)
        await self.repos.members.set_role(workspace_id, user_id, role)
        member.role = role
        await self.repos.activity.record(
            verb="member.role_changed",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            data={"user_id": user_id, "role": role.value},
        )
        return member

    async def remove_member(
        self, *, actor_user_id: str, workspace_id: str, user_id: str
    ) -> None:
        """Soft-remove a member (needs MANAGE_MEMBERS). The org owner cannot be removed."""
        ws = await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(workspace_id)
        )
        org = await self.repos.orgs.get(ws.org_id)
        if org is not None and org.owner_user_id == user_id:
            raise WorkspaceError(
                "cannot_remove_owner", "the organization owner cannot be removed", status=409
            )
        await self.repos.members.set_status(workspace_id, user_id, MemberStatus.REMOVED)
        await self.repos.activity.record(
            verb="member.removed",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            data={"user_id": user_id},
        )

    async def list_members(self, workspace_id: str) -> list[WorkspaceMember]:
        """All active members of a workspace."""
        return await self.repos.members.list_for_workspace(
            workspace_id, status=MemberStatus.ACTIVE
        )

    # ------------------------------------------------------------------ #
    # Invitations (the email-token accept flow)
    # ------------------------------------------------------------------ #

    async def invite_member(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        email: str,
        role: Role | None = None,
        ttl_s: int = DEFAULT_INVITE_TTL_S,
    ) -> InvitationResult:
        """Issue a signed email-token invitation (needs MANAGE_MEMBERS)."""
        ws = await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(workspace_id)
        )
        await self._guard_seat(workspace_id)
        email = email.strip().lower()
        invite_role = role or default_member_role_for(ws.settings)

        # Idempotent: re-inviting a pending email re-issues a fresh token.
        existing = await self.repos.invitations.find_pending(workspace_id, email)
        if existing is not None:
            await self.repos.invitations.set_status(existing.id, InvitationStatus.REVOKED)

        invitation_id = new_invitation_id()
        token, expires_at = create_invitation_token(
            invitation_id=invitation_id,
            workspace_id=workspace_id,
            email=email,
            role=invite_role,
            secret=self._invite_secret,
            ttl_s=ttl_s,
        )
        invitation = await self.repos.invitations.create(
            invitation_id=invitation_id,
            workspace_id=workspace_id,
            email=email,
            role=invite_role,
            token=token,
            expires_at=expires_at,
            invited_by_user_id=actor_user_id,
        )
        await self.repos.activity.record(
            verb="member.invited",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            data={"email": email, "role": invite_role.value, "invitation_id": invitation_id},
        )
        return InvitationResult(invitation=invitation, token=token)

    async def accept_invitation(
        self, *, token: str, accepting_user_id: str
    ) -> WorkspaceMember:
        """Accept an invitation token, joining the accepting user to the workspace.

        Verifies the token's signature + expiry, cross-checks the stored row, and
        (defence-in-depth) requires the accepting user's email to match the invitee.
        """
        claims = verify_invitation_token(token, self._invite_secret)
        invitation = await self.repos.invitations.get(claims.invitation_id)
        if invitation is None or invitation.token != token:
            raise WorkspaceError("invitation_not_found", "no such invitation", status=404)
        if invitation.status != InvitationStatus.PENDING:
            raise WorkspaceError(
                "invitation_not_pending",
                f"invitation is {invitation.status.value}",
                status=409,
            )
        if invitation.expires_at < datetime.now(UTC):
            await self.repos.invitations.set_status(invitation.id, InvitationStatus.EXPIRED)
            raise WorkspaceError("invitation_expired", "invitation has expired", status=409)

        user = await self._users.get(accepting_user_id)
        if user is None:
            raise WorkspaceError("user_not_found", "no such user", status=404)
        if user.email.strip().lower() != invitation.email:
            raise WorkspaceError(
                "invitation_email_mismatch",
                "this invitation was issued to a different email",
                status=403,
            )

        await self._guard_seat(invitation.workspace_id, adding_user_id=accepting_user_id)
        member = await self.repos.members.upsert(
            workspace_id=invitation.workspace_id,
            user_id=accepting_user_id,
            role=invitation.role,
            status=MemberStatus.ACTIVE,
            invited_by_user_id=invitation.invited_by_user_id,
        )
        await self.repos.invitations.mark_accepted(invitation.id, accepting_user_id)
        await self.repos.activity.record(
            verb="member.joined",
            workspace_id=invitation.workspace_id,
            actor_user_id=accepting_user_id,
            data={"role": invitation.role.value, "invitation_id": invitation.id},
        )
        return member

    async def revoke_invitation(
        self, *, actor_user_id: str, invitation_id: str
    ) -> None:
        """Revoke a pending invitation (needs MANAGE_MEMBERS on the workspace)."""
        invitation = await self.repos.invitations.get(invitation_id)
        if invitation is None:
            raise WorkspaceError("invitation_not_found", "no such invitation", status=404)
        await self.authz.require(
            actor_user_id, Action.MANAGE_MEMBERS, ResourceRef.workspace(invitation.workspace_id)
        )
        await self.repos.invitations.set_status(invitation_id, InvitationStatus.REVOKED)
        await self.repos.activity.record(
            verb="member.invite_revoked",
            workspace_id=invitation.workspace_id,
            actor_user_id=actor_user_id,
            data={"invitation_id": invitation_id, "email": invitation.email},
        )

    async def list_pending_invitations(self, workspace_id: str) -> list[WorkspaceInvitation]:
        """Pending invitations for a workspace (lazily expiring stale ones first)."""
        await self.repos.invitations.expire_stale()
        return await self.repos.invitations.list_pending_for_workspace(workspace_id)

    # ------------------------------------------------------------------ #
    # Shared library — attaching books + direct shares
    # ------------------------------------------------------------------ #

    async def attach_book(
        self, *, actor_user_id: str, workspace_id: str, book_id: str
    ) -> None:
        """Attach a book to a workspace's shared shelf.

        The actor must be able to SHARE the book (own it or hold an owner share)
        and MANAGE_COLLECTIONS in the target workspace. Honours the workspace's
        ``max_books`` quota.
        """
        ws = await self._require_workspace(workspace_id)
        book = await self.session.get(Book, book_id)
        if book is None:
            raise WorkspaceError("book_not_found", "no such book", status=404)
        await self.authz.require(actor_user_id, Action.SHARE, ResourceRef.book(book_id))
        await self.authz.require(
            actor_user_id, Action.MANAGE_COLLECTIONS, ResourceRef.workspace(workspace_id)
        )
        current = await self.repos.workspace_books.count_for_workspace(workspace_id)
        check_book_quota(ws.settings, current)
        await self.repos.workspace_books.attach(
            workspace_id=workspace_id, book_id=book_id, added_by_user_id=actor_user_id
        )
        await self.repos.activity.record(
            verb="book.attached",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.BOOK,
            resource_id=book_id,
        )

    async def detach_book(
        self, *, actor_user_id: str, workspace_id: str, book_id: str
    ) -> None:
        """Detach a book from a workspace (needs MANAGE_COLLECTIONS)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_COLLECTIONS, ResourceRef.workspace(workspace_id)
        )
        await self.repos.workspace_books.detach(workspace_id, book_id)
        await self.repos.activity.record(
            verb="book.detached",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.BOOK,
            resource_id=book_id,
        )

    async def share_resource(
        self,
        *,
        actor_user_id: str,
        resource: ResourceRef,
        grantee_user_id: str,
        role: Role,
        expires_at: datetime | None = None,
    ) -> ResourceShare:
        """Grant a direct role to a user on a resource (needs SHARE on it)."""
        await self.authz.require(actor_user_id, Action.SHARE, resource)
        if grantee_user_id == actor_user_id:
            raise WorkspaceError("cannot_share_with_self", "cannot share with yourself")
        grantee = await self._users.get(grantee_user_id)
        if grantee is None:
            raise WorkspaceError("user_not_found", "no such grantee", status=404)
        share = await self.repos.shares.upsert(
            resource_type=resource.type,
            resource_id=resource.id,
            user_id=grantee_user_id,
            role=role,
            granted_by_user_id=actor_user_id,
            expires_at=expires_at,
        )
        await self.repos.activity.record(
            verb="resource.shared",
            actor_user_id=actor_user_id,
            resource_type=resource.type,
            resource_id=resource.id,
            data={"grantee": grantee_user_id, "role": role.value},
        )
        return share

    async def share_resource_by_email(
        self,
        *,
        actor_user_id: str,
        resource: ResourceRef,
        email: str,
        role: Role,
    ) -> ResourceShare:
        """Share with an *existing* user resolved by email (404 if no account)."""
        grantee = await self._users.get_by_email(email.strip().lower())
        if grantee is None:
            raise WorkspaceError(
                "user_not_found", "no account with that email; invite them first", status=404
            )
        return await self.share_resource(
            actor_user_id=actor_user_id,
            resource=resource,
            grantee_user_id=grantee.id,
            role=role,
        )

    async def revoke_share(
        self, *, actor_user_id: str, resource: ResourceRef, grantee_user_id: str
    ) -> None:
        """Revoke a direct share (needs SHARE on the resource)."""
        await self.authz.require(actor_user_id, Action.SHARE, resource)
        revoked = await self.repos.shares.revoke(resource.type, resource.id, grantee_user_id)
        if not revoked:
            raise WorkspaceError("share_not_found", "no such share", status=404)
        await self.repos.activity.record(
            verb="resource.unshared",
            actor_user_id=actor_user_id,
            resource_type=resource.type,
            resource_id=resource.id,
            data={"grantee": grantee_user_id},
        )

    async def list_resource_shares(
        self, *, actor_user_id: str, resource: ResourceRef
    ) -> list[ResourceShare]:
        """List the direct shares on a resource (needs SHARE — i.e. owner-level)."""
        await self.authz.require(actor_user_id, Action.SHARE, resource)
        return await self.repos.shares.list_for_resource(resource.type, resource.id)

    async def list_shared_books_for_user(self, user_id: str) -> list[str]:
        """Book ids a user can reach via *any* path (shares + workspace shelves).

        The composable answer to "what's on my shelf, including what's shared with
        me" — distinct from ``BookRepo.list_for_user`` (personal-owned only).
        """
        book_ids: set[str] = set()
        book_shares = await self.repos.shares.list_for_user(
            user_id, resource_type=ResourceType.BOOK
        )
        for share in book_shares:
            book_ids.add(share.resource_id)
        for member in await self.repos.members.list_for_user(user_id):
            for bid in await self.repos.workspace_books.list_book_ids(member.workspace_id):
                book_ids.add(bid)
        for org in await self.repos.orgs.list_for_owner(user_id):
            for ws in await self.repos.workspaces.list_for_org(org.id):
                for bid in await self.repos.workspace_books.list_book_ids(ws.id):
                    book_ids.add(bid)
        return sorted(book_ids)

    # ------------------------------------------------------------------ #
    # Collections
    # ------------------------------------------------------------------ #

    async def create_collection(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        name: str,
        description: str | None = None,
    ) -> Collection:
        """Create a named collection inside a workspace (needs MANAGE_COLLECTIONS)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.MANAGE_COLLECTIONS, ResourceRef.workspace(workspace_id)
        )
        slug = await self._unique_collection_slug(workspace_id, slugify(name))
        coll = await self.repos.collections.create(
            workspace_id=workspace_id,
            name=name,
            slug=slug,
            description=description,
            created_by_user_id=actor_user_id,
        )
        await self.repos.activity.record(
            verb="collection.created",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.COLLECTION,
            resource_id=coll.id,
            data={"name": name},
        )
        return coll

    async def add_to_collection(
        self, *, actor_user_id: str, collection_id: str, book_id: str
    ) -> None:
        """Add a book to a collection (needs MANAGE_COLLECTIONS on the collection)."""
        coll = await self.repos.collections.get(collection_id)
        if coll is None:
            raise WorkspaceError("collection_not_found", "no such collection", status=404)
        await self.authz.require(
            actor_user_id, Action.MANAGE_COLLECTIONS, ResourceRef.collection(collection_id)
        )
        await self.repos.collections.add_item(collection_id=collection_id, book_id=book_id)
        await self.repos.activity.record(
            verb="collection.book_added",
            workspace_id=coll.workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.COLLECTION,
            resource_id=collection_id,
            data={"book_id": book_id},
        )

    async def remove_from_collection(
        self, *, actor_user_id: str, collection_id: str, book_id: str
    ) -> None:
        """Remove a book from a collection (needs MANAGE_COLLECTIONS)."""
        coll = await self.repos.collections.get(collection_id)
        if coll is None:
            raise WorkspaceError("collection_not_found", "no such collection", status=404)
        await self.authz.require(
            actor_user_id, Action.MANAGE_COLLECTIONS, ResourceRef.collection(collection_id)
        )
        await self.repos.collections.remove_item(collection_id, book_id)
        await self.repos.activity.record(
            verb="collection.book_removed",
            workspace_id=coll.workspace_id,
            actor_user_id=actor_user_id,
            resource_type=ResourceType.COLLECTION,
            resource_id=collection_id,
            data={"book_id": book_id},
        )

    # ------------------------------------------------------------------ #
    # Transfer of ownership
    # ------------------------------------------------------------------ #

    async def request_transfer(
        self,
        *,
        actor_user_id: str,
        resource: ResourceRef,
        to_user_id: str,
        note: str | None = None,
    ) -> OwnershipTransfer:
        """Open a transfer-of-ownership request (needs TRANSFER_OWNERSHIP on it)."""
        await self.authz.require(actor_user_id, Action.TRANSFER_OWNERSHIP, resource)
        if to_user_id == actor_user_id:
            raise WorkspaceError("cannot_transfer_to_self", "already the owner")
        recipient = await self._users.get(to_user_id)
        if recipient is None:
            raise WorkspaceError("user_not_found", "no such recipient", status=404)
        if await self.repos.transfers.find_pending(resource.type, resource.id) is not None:
            raise WorkspaceError(
                "transfer_pending", "a transfer is already pending for this resource", status=409
            )
        transfer = await self.repos.transfers.create(
            resource_type=resource.type,
            resource_id=resource.id,
            from_user_id=actor_user_id,
            to_user_id=to_user_id,
            note=note,
        )
        await self.repos.activity.record(
            verb="transfer.requested",
            actor_user_id=actor_user_id,
            resource_type=resource.type,
            resource_id=resource.id,
            data={"to_user_id": to_user_id, "transfer_id": transfer.id},
        )
        return transfer

    async def respond_to_transfer(
        self, *, actor_user_id: str, transfer_id: str, accept: bool
    ) -> OwnershipTransfer:
        """The recipient accepts/declines a pending transfer (recipient only)."""
        transfer = await self.repos.transfers.get(transfer_id)
        if transfer is None:
            raise WorkspaceError("transfer_not_found", "no such transfer", status=404)
        if transfer.status != TransferStatus.PENDING:
            raise WorkspaceError("transfer_resolved", "transfer already resolved", status=409)
        if transfer.to_user_id != actor_user_id:
            raise WorkspaceError(
                "forbidden", "only the recipient can respond to this transfer", status=403
            )
        if not accept:
            await self.repos.transfers.resolve(transfer_id, TransferStatus.DECLINED)
            transfer.status = TransferStatus.DECLINED
            await self.repos.activity.record(
                verb="transfer.declined",
                actor_user_id=actor_user_id,
                resource_type=transfer.resource_type,
                resource_id=transfer.resource_id,
                data={"transfer_id": transfer_id},
            )
            return transfer

        await self._apply_ownership(transfer)
        await self.repos.transfers.resolve(transfer_id, TransferStatus.ACCEPTED)
        transfer.status = TransferStatus.ACCEPTED
        await self.repos.activity.record(
            verb="transfer.accepted",
            actor_user_id=actor_user_id,
            resource_type=transfer.resource_type,
            resource_id=transfer.resource_id,
            data={"transfer_id": transfer_id, "from": transfer.from_user_id},
        )
        return transfer

    async def cancel_transfer(
        self, *, actor_user_id: str, transfer_id: str
    ) -> OwnershipTransfer:
        """The requester cancels a pending transfer (requester only)."""
        transfer = await self.repos.transfers.get(transfer_id)
        if transfer is None:
            raise WorkspaceError("transfer_not_found", "no such transfer", status=404)
        if transfer.status != TransferStatus.PENDING:
            raise WorkspaceError("transfer_resolved", "transfer already resolved", status=409)
        if transfer.from_user_id != actor_user_id:
            raise WorkspaceError(
                "forbidden", "only the requester can cancel this transfer", status=403
            )
        await self.repos.transfers.resolve(transfer_id, TransferStatus.CANCELLED)
        transfer.status = TransferStatus.CANCELLED
        await self.repos.activity.record(
            verb="transfer.cancelled",
            actor_user_id=actor_user_id,
            resource_type=transfer.resource_type,
            resource_id=transfer.resource_id,
            data={"transfer_id": transfer_id},
        )
        return transfer

    async def _apply_ownership(self, transfer: OwnershipTransfer) -> None:
        """Move the durable owner bit for the resource to ``transfer.to_user_id``."""
        if transfer.resource_type == ResourceType.BOOK:
            book = await self.session.get(Book, transfer.resource_id)
            if book is None:
                raise WorkspaceError("book_not_found", "book no longer exists", status=404)
            book.user_id = transfer.to_user_id
            await self.session.flush()
        elif transfer.resource_type == ResourceType.WORKSPACE:
            ws = await self.repos.workspaces.get(transfer.resource_id)
            if ws is None:
                raise WorkspaceError(
                    "workspace_not_found", "workspace no longer exists", status=404
                )
            await self.repos.members.upsert(
                workspace_id=ws.id,
                user_id=transfer.to_user_id,
                role=Role.OWNER,
                status=MemberStatus.ACTIVE,
            )
            await self.repos.orgs.set_owner(ws.org_id, transfer.to_user_id)
        # Collections own no durable owner column; their transfer is recorded only.

    # ------------------------------------------------------------------ #
    # Activity feed
    # ------------------------------------------------------------------ #

    async def activity_feed(
        self, *, actor_user_id: str, workspace_id: str, limit: int = 50
    ) -> list:
        """Read a workspace's activity feed (needs VIEW_ACTIVITY)."""
        await self._require_workspace(workspace_id)
        await self.authz.require(
            actor_user_id, Action.VIEW_ACTIVITY, ResourceRef.workspace(workspace_id)
        )
        return await self.repos.activity.list_for_workspace(workspace_id, limit=limit)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _require_workspace(self, workspace_id: str) -> Workspace:
        ws = await self.repos.workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceError("workspace_not_found", "no such workspace", status=404)
        return ws

    async def _require_org_owner(self, actor_user_id: str, org_id: str) -> Organization:
        org = await self.repos.orgs.get(org_id)
        if org is None:
            raise WorkspaceError("org_not_found", "no such organization", status=404)
        if org.owner_user_id != actor_user_id:
            raise WorkspaceError("forbidden", "only the org owner may do this", status=403)
        return org

    async def _guard_seat(
        self, workspace_id: str, *, adding_user_id: str | None = None
    ) -> None:
        """Block a new seat when the org is at capacity.

        A seat is consumed per *distinct active user across the org*, so adding a
        user who is already active in any of the org's workspaces consumes no new
        seat — ``adding_user_id`` lets that case be a no-op rather than a false
        positive. (The org owner always holds a seat via their owning workspace.)
        """
        ws = await self.repos.workspaces.get(workspace_id)
        if ws is None:
            return
        org = await self.repos.orgs.get(ws.org_id)
        if org is None:
            return
        if adding_user_id is not None and await self._user_active_in_org(
            org.id, adding_user_id
        ):
            return  # already counted toward the org's seats; no new seat consumed
        active = await self.repos.members.count_active_in_org(org.id)
        check_seat_quota(SeatUsage(seats=org.seats, active_members=active), adding=1)

    async def _user_active_in_org(self, org_id: str, user_id: str) -> bool:
        """True when ``user_id`` is an active member of any workspace under the org."""
        for ws in await self.repos.workspaces.list_for_org(org_id, include_archived=True):
            if await self.repos.members.get_active(ws.id, user_id) is not None:
                return True
        return False

    async def _unique_workspace_slug(self, org_id: str, base: str) -> str:
        slug = base
        suffix = 2
        while await self.repos.workspaces.get_by_slug(org_id, slug) is not None:
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug

    async def _unique_collection_slug(self, workspace_id: str, base: str) -> str:
        existing = {c.slug for c in await self.repos.collections.list_for_workspace(workspace_id)}
        slug = base
        suffix = 2
        while slug in existing:
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug


__all__ = [
    "InvitationResult",
    "WorkspaceError",
    "WorkspaceService",
    "slugify",
]
