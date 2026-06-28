"""Repositories for the workspaces subsystem.

Each repository wraps an :class:`AsyncSession` and owns the queries for one
aggregate. They ``flush`` (to populate defaults / surface constraint errors and
make rows queryable within the transaction) but never ``commit`` — the unit-of-work
boundary (the request / service layer) owns the transaction. This mirrors
:class:`app.db.repositories.base.BaseRepository`, re-used directly so the
workspaces package follows the house data-access pattern exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.db.repositories.base import BaseRepository
from app.workspaces.models import (
    Collection,
    CollectionItem,
    Organization,
    OwnershipTransfer,
    ResourceShare,
    Workspace,
    WorkspaceActivity,
    WorkspaceBook,
    WorkspaceInvitation,
    WorkspaceMember,
)
from app.workspaces.roles import (
    InvitationStatus,
    MemberStatus,
    OrgPlan,
    ResourceType,
    Role,
    TransferStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Organizations
# --------------------------------------------------------------------------- #


class OrganizationRepo(BaseRepository):
    """Create + query organizations (the tenant + seat owner)."""

    async def create(
        self,
        *,
        name: str,
        owner_user_id: str | None,
        plan: OrgPlan = OrgPlan.FREE,
        seats: int = 5,
        org_id: str | None = None,
    ) -> Organization:
        org = Organization(
            id=org_id or new_id(),
            name=name,
            owner_user_id=owner_user_id,
            plan=plan,
            seats=seats,
            settings={},
        )
        self.session.add(org)
        await self.session.flush()
        return org

    async def get(self, org_id: str) -> Organization | None:
        return await self.session.get(Organization, org_id)

    async def list_for_owner(self, owner_user_id: str) -> list[Organization]:
        stmt = (
            select(Organization)
            .where(Organization.owner_user_id == owner_user_id)
            .order_by(Organization.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_seats(self, org_id: str, seats: int) -> None:
        await self.session.execute(
            update(Organization).where(Organization.id == org_id).values(seats=seats)
        )
        await self.session.flush()

    async def set_owner(self, org_id: str, owner_user_id: str | None) -> None:
        await self.session.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(owner_user_id=owner_user_id)
        )
        await self.session.flush()


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #


class WorkspaceRepo(BaseRepository):
    """Create + query workspaces (the shared shelves)."""

    async def create(
        self,
        *,
        org_id: str,
        name: str,
        slug: str,
        description: str | None = None,
        settings: dict | None = None,
        workspace_id: str | None = None,
    ) -> Workspace:
        ws = Workspace(
            id=workspace_id or new_id(),
            org_id=org_id,
            name=name,
            slug=slug,
            description=description,
            settings=settings or {},
            archived=False,
        )
        self.session.add(ws)
        await self.session.flush()
        return ws

    async def get(self, workspace_id: str) -> Workspace | None:
        return await self.session.get(Workspace, workspace_id)

    async def get_by_slug(self, org_id: str, slug: str) -> Workspace | None:
        stmt = select(Workspace).where(
            Workspace.org_id == org_id, Workspace.slug == slug
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_org(self, org_id: str, *, include_archived: bool = False) -> list[Workspace]:
        stmt = select(Workspace).where(Workspace.org_id == org_id)
        if not include_archived:
            stmt = stmt.where(Workspace.archived.is_(False))
        stmt = stmt.order_by(Workspace.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def update_settings(self, workspace_id: str, settings: dict) -> None:
        await self.session.execute(
            update(Workspace).where(Workspace.id == workspace_id).values(settings=settings)
        )
        await self.session.flush()

    async def update_fields(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
    ) -> None:
        values: dict = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if archived is not None:
            values["archived"] = archived
        if values:
            await self.session.execute(
                update(Workspace).where(Workspace.id == workspace_id).values(**values)
            )
            await self.session.flush()

    async def delete(self, workspace_id: str) -> None:
        ws = await self.get(workspace_id)
        if ws is not None:
            await self.session.delete(ws)
            await self.session.flush()


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #


class MemberRepo(BaseRepository):
    """The (workspace, user) membership edges."""

    async def upsert(
        self,
        *,
        workspace_id: str,
        user_id: str,
        role: Role,
        status: MemberStatus = MemberStatus.ACTIVE,
        invited_by_user_id: str | None = None,
    ) -> WorkspaceMember:
        """Create or update a member's role/status (idempotent on re-invite)."""
        existing = await self.get(workspace_id, user_id)
        if existing is not None:
            existing.role = role
            existing.status = status
            if invited_by_user_id is not None:
                existing.invited_by_user_id = invited_by_user_id
            await self.session.flush()
            return existing
        member = WorkspaceMember(
            id=new_id(),
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            status=status,
            invited_by_user_id=invited_by_user_id,
        )
        self.session.add(member)
        await self.session.flush()
        return member

    async def get(self, workspace_id: str, user_id: str) -> WorkspaceMember | None:
        stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_active(self, workspace_id: str, user_id: str) -> WorkspaceMember | None:
        """Fetch the member only when their membership is live (active)."""
        member = await self.get(workspace_id, user_id)
        if member is not None and member.status == MemberStatus.ACTIVE:
            return member
        return None

    async def list_for_workspace(
        self, workspace_id: str, *, status: MemberStatus | None = None
    ) -> list[WorkspaceMember]:
        stmt = select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(WorkspaceMember.status == status)
        stmt = stmt.order_by(WorkspaceMember.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(self, user_id: str) -> list[WorkspaceMember]:
        stmt = (
            select(WorkspaceMember)
            .where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.status == MemberStatus.ACTIVE,
            )
            .order_by(WorkspaceMember.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_active(self, workspace_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.status == MemberStatus.ACTIVE,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_active_in_org(self, org_id: str) -> int:
        """Distinct active members across all of an org's workspaces (the seat count)."""
        stmt = (
            select(func.count(func.distinct(WorkspaceMember.user_id)))
            .select_from(WorkspaceMember)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .where(
                Workspace.org_id == org_id,
                WorkspaceMember.status == MemberStatus.ACTIVE,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def set_status(
        self, workspace_id: str, user_id: str, status: MemberStatus
    ) -> None:
        await self.session.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .values(status=status)
        )
        await self.session.flush()

    async def set_role(self, workspace_id: str, user_id: str, role: Role) -> None:
        await self.session.execute(
            update(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .values(role=role)
        )
        await self.session.flush()


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #


class InvitationRepo(BaseRepository):
    """The email-token invitation rows."""

    async def create(
        self,
        *,
        invitation_id: str,
        workspace_id: str,
        email: str,
        role: Role,
        token: str,
        expires_at: datetime,
        invited_by_user_id: str | None = None,
    ) -> WorkspaceInvitation:
        inv = WorkspaceInvitation(
            id=invitation_id,
            workspace_id=workspace_id,
            email=email.strip().lower(),
            role=role,
            token=token,
            status=InvitationStatus.PENDING,
            invited_by_user_id=invited_by_user_id,
            expires_at=expires_at,
        )
        self.session.add(inv)
        await self.session.flush()
        return inv

    async def get(self, invitation_id: str) -> WorkspaceInvitation | None:
        return await self.session.get(WorkspaceInvitation, invitation_id)

    async def get_by_token(self, token: str) -> WorkspaceInvitation | None:
        stmt = select(WorkspaceInvitation).where(WorkspaceInvitation.token == token)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_pending_for_workspace(self, workspace_id: str) -> list[WorkspaceInvitation]:
        stmt = (
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.status == InvitationStatus.PENDING,
            )
            .order_by(WorkspaceInvitation.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_pending(self, workspace_id: str, email: str) -> WorkspaceInvitation | None:
        stmt = select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.email == email.strip().lower(),
            WorkspaceInvitation.status == InvitationStatus.PENDING,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def mark_accepted(self, invitation_id: str, accepted_by_user_id: str) -> None:
        await self.session.execute(
            update(WorkspaceInvitation)
            .where(WorkspaceInvitation.id == invitation_id)
            .values(
                status=InvitationStatus.ACCEPTED,
                accepted_at=_utcnow(),
                accepted_by_user_id=accepted_by_user_id,
            )
        )
        await self.session.flush()

    async def set_status(self, invitation_id: str, status: InvitationStatus) -> None:
        await self.session.execute(
            update(WorkspaceInvitation)
            .where(WorkspaceInvitation.id == invitation_id)
            .values(status=status)
        )
        await self.session.flush()

    async def expire_stale(self, *, now: datetime | None = None) -> int:
        """Flip pending invitations past their expiry to EXPIRED; return the count."""
        moment = now or _utcnow()
        result = await self.session.execute(
            update(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.status == InvitationStatus.PENDING,
                WorkspaceInvitation.expires_at < moment,
            )
            .values(status=InvitationStatus.EXPIRED)
        )
        await self.session.flush()
        # ``rowcount`` is on the cursor result for a bulk UPDATE (typed as Result).
        return int(getattr(result, "rowcount", 0) or 0)


# --------------------------------------------------------------------------- #
# Resource shares
# --------------------------------------------------------------------------- #


class ResourceShareRepo(BaseRepository):
    """Direct (resource, user) → role grants (polymorphic over book/collection/workspace)."""

    async def upsert(
        self,
        *,
        resource_type: ResourceType,
        resource_id: str,
        user_id: str,
        role: Role,
        granted_by_user_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> ResourceShare:
        existing = await self.get(resource_type, resource_id, user_id)
        if existing is not None:
            existing.role = role
            existing.granted_by_user_id = granted_by_user_id
            existing.expires_at = expires_at
            await self.session.flush()
            return existing
        share = ResourceShare(
            id=new_id(),
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=user_id,
            role=role,
            granted_by_user_id=granted_by_user_id,
            expires_at=expires_at,
        )
        self.session.add(share)
        await self.session.flush()
        return share

    async def get(
        self, resource_type: ResourceType, resource_id: str, user_id: str
    ) -> ResourceShare | None:
        stmt = select(ResourceShare).where(
            ResourceShare.resource_type == resource_type,
            ResourceShare.resource_id == resource_id,
            ResourceShare.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_live(
        self,
        resource_type: ResourceType,
        resource_id: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> ResourceShare | None:
        """A share that exists and is not past its expiry (the authz path)."""
        share = await self.get(resource_type, resource_id, user_id)
        if share is None:
            return None
        if share.expires_at is not None and share.expires_at < (now or _utcnow()):
            return None
        return share

    async def list_for_resource(
        self, resource_type: ResourceType, resource_id: str
    ) -> list[ResourceShare]:
        stmt = (
            select(ResourceShare)
            .where(
                ResourceShare.resource_type == resource_type,
                ResourceShare.resource_id == resource_id,
            )
            .order_by(ResourceShare.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(
        self, user_id: str, *, resource_type: ResourceType | None = None
    ) -> list[ResourceShare]:
        stmt = select(ResourceShare).where(ResourceShare.user_id == user_id)
        if resource_type is not None:
            stmt = stmt.where(ResourceShare.resource_type == resource_type)
        stmt = stmt.order_by(ResourceShare.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def revoke(
        self, resource_type: ResourceType, resource_id: str, user_id: str
    ) -> bool:
        share = await self.get(resource_type, resource_id, user_id)
        if share is None:
            return False
        await self.session.delete(share)
        await self.session.flush()
        return True


# --------------------------------------------------------------------------- #
# Workspace ↔ book attachment
# --------------------------------------------------------------------------- #


class WorkspaceBookRepo(BaseRepository):
    """Which books are attached to which workspaces (the shared shelf membership)."""

    async def attach(
        self, *, workspace_id: str, book_id: str, added_by_user_id: str | None = None
    ) -> WorkspaceBook:
        existing = await self.get(workspace_id, book_id)
        if existing is not None:
            return existing
        row = WorkspaceBook(
            id=new_id(),
            workspace_id=workspace_id,
            book_id=book_id,
            added_by_user_id=added_by_user_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, workspace_id: str, book_id: str) -> WorkspaceBook | None:
        stmt = select(WorkspaceBook).where(
            WorkspaceBook.workspace_id == workspace_id,
            WorkspaceBook.book_id == book_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_book_ids(self, workspace_id: str) -> list[str]:
        stmt = select(WorkspaceBook.book_id).where(
            WorkspaceBook.workspace_id == workspace_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def workspaces_for_book(self, book_id: str) -> list[str]:
        stmt = select(WorkspaceBook.workspace_id).where(WorkspaceBook.book_id == book_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for_workspace(self, workspace_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(WorkspaceBook)
            .where(WorkspaceBook.workspace_id == workspace_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def detach(self, workspace_id: str, book_id: str) -> bool:
        row = await self.get(workspace_id, book_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


class CollectionRepo(BaseRepository):
    """Named bundles of books inside a workspace."""

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        slug: str,
        description: str | None = None,
        created_by_user_id: str | None = None,
        collection_id: str | None = None,
    ) -> Collection:
        coll = Collection(
            id=collection_id or new_id(),
            workspace_id=workspace_id,
            name=name,
            slug=slug,
            description=description,
            created_by_user_id=created_by_user_id,
        )
        self.session.add(coll)
        await self.session.flush()
        return coll

    async def get(self, collection_id: str) -> Collection | None:
        return await self.session.get(Collection, collection_id)

    async def list_for_workspace(self, workspace_id: str) -> list[Collection]:
        stmt = (
            select(Collection)
            .where(Collection.workspace_id == workspace_id)
            .order_by(Collection.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_item(
        self, *, collection_id: str, book_id: str, position: int = 0
    ) -> CollectionItem:
        existing = await self._get_item(collection_id, book_id)
        if existing is not None:
            return existing
        item = CollectionItem(
            id=new_id(), collection_id=collection_id, book_id=book_id, position=position
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def _get_item(self, collection_id: str, book_id: str) -> CollectionItem | None:
        stmt = select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.book_id == book_id,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_items(self, collection_id: str) -> list[CollectionItem]:
        stmt = (
            select(CollectionItem)
            .where(CollectionItem.collection_id == collection_id)
            .order_by(CollectionItem.position.asc(), CollectionItem.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def remove_item(self, collection_id: str, book_id: str) -> bool:
        item = await self._get_item(collection_id, book_id)
        if item is None:
            return False
        await self.session.delete(item)
        await self.session.flush()
        return True

    async def delete(self, collection_id: str) -> None:
        coll = await self.get(collection_id)
        if coll is not None:
            await self.session.delete(coll)
            await self.session.flush()


# --------------------------------------------------------------------------- #
# Ownership transfers
# --------------------------------------------------------------------------- #


class OwnershipTransferRepo(BaseRepository):
    """Audited transfer-of-ownership requests + their resolution."""

    async def create(
        self,
        *,
        resource_type: ResourceType,
        resource_id: str,
        from_user_id: str | None,
        to_user_id: str,
        note: str | None = None,
        transfer_id: str | None = None,
    ) -> OwnershipTransfer:
        transfer = OwnershipTransfer(
            id=transfer_id or new_id(),
            resource_type=resource_type,
            resource_id=resource_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            status=TransferStatus.PENDING,
            note=note,
        )
        self.session.add(transfer)
        await self.session.flush()
        return transfer

    async def get(self, transfer_id: str) -> OwnershipTransfer | None:
        return await self.session.get(OwnershipTransfer, transfer_id)

    async def find_pending(
        self, resource_type: ResourceType, resource_id: str
    ) -> OwnershipTransfer | None:
        stmt = select(OwnershipTransfer).where(
            OwnershipTransfer.resource_type == resource_type,
            OwnershipTransfer.resource_id == resource_id,
            OwnershipTransfer.status == TransferStatus.PENDING,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_incoming(self, to_user_id: str) -> list[OwnershipTransfer]:
        stmt = (
            select(OwnershipTransfer)
            .where(
                OwnershipTransfer.to_user_id == to_user_id,
                OwnershipTransfer.status == TransferStatus.PENDING,
            )
            .order_by(OwnershipTransfer.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def resolve(self, transfer_id: str, status: TransferStatus) -> None:
        await self.session.execute(
            update(OwnershipTransfer)
            .where(OwnershipTransfer.id == transfer_id)
            .values(status=status, resolved_at=_utcnow())
        )
        await self.session.flush()


# --------------------------------------------------------------------------- #
# Activity feed
# --------------------------------------------------------------------------- #


class ActivityRepo(BaseRepository):
    """Append-only activity feed writes + reads."""

    async def record(
        self,
        *,
        verb: str,
        workspace_id: str | None = None,
        actor_user_id: str | None = None,
        resource_type: ResourceType | None = None,
        resource_id: str | None = None,
        data: dict | None = None,
    ) -> WorkspaceActivity:
        row = WorkspaceActivity(
            id=new_id(),
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            verb=verb,
            resource_type=resource_type,
            resource_id=resource_id,
            data=data or {},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_workspace(
        self, workspace_id: str, *, limit: int = 50, before: datetime | None = None
    ) -> list[WorkspaceActivity]:
        stmt = select(WorkspaceActivity).where(
            WorkspaceActivity.workspace_id == workspace_id
        )
        if before is not None:
            stmt = stmt.where(WorkspaceActivity.created_at < before)
        stmt = stmt.order_by(WorkspaceActivity.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_resource(
        self, resource_type: ResourceType, resource_id: str, *, limit: int = 50
    ) -> list[WorkspaceActivity]:
        stmt = (
            select(WorkspaceActivity)
            .where(
                WorkspaceActivity.resource_type == resource_type,
                WorkspaceActivity.resource_id == resource_id,
            )
            .order_by(WorkspaceActivity.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# A small convenience bundle (so callers grab one handle, not eight)
# --------------------------------------------------------------------------- #


class WorkspaceRepos:
    """All workspace repositories bound to one session (a tidy aggregate handle)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orgs = OrganizationRepo(session)
        self.workspaces = WorkspaceRepo(session)
        self.members = MemberRepo(session)
        self.invitations = InvitationRepo(session)
        self.shares = ResourceShareRepo(session)
        self.workspace_books = WorkspaceBookRepo(session)
        self.collections = CollectionRepo(session)
        self.transfers = OwnershipTransferRepo(session)
        self.activity = ActivityRepo(session)


__all__ = [
    "ActivityRepo",
    "CollectionRepo",
    "InvitationRepo",
    "MemberRepo",
    "OrganizationRepo",
    "OwnershipTransferRepo",
    "ResourceShareRepo",
    "WorkspaceBookRepo",
    "WorkspaceRepo",
    "WorkspaceRepos",
]
