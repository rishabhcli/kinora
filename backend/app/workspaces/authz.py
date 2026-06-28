"""The composable authorization service — ``can(user, action, resource)``.

This is the **public** authz API other domains call. It is the DB-backed
*resolution* half of the model: given a user, an action, and a resource, it
resolves every live grant the user holds on that resource (by personal-owner,
direct-share, workspace-membership, and org-owner paths), folds them into a
:class:`~app.workspaces.policy.GrantSet`, and delegates the allow/deny decision
to the pure :func:`~app.workspaces.policy.decide`. Keeping resolution and decision
separate means the rules themselves stay exhaustively unit-testable without infra.

Composition with the existing model (kinora.md §5.1):

* the durable ``books.user_id`` owner is always resolved to OWNER of that book —
  *additive*; a workspace can add collaborators but can never lock the personal
  owner out of their own book;
* a book reachable through a workspace (``workspace_books``) inherits the user's
  active workspace-membership role;
* a direct ``resource_shares`` row is the strongest non-owner path;
* the organization owner is OWNER of every workspace beneath the org (and, by
  extension, of the books attached to those workspaces).

The service takes a :class:`ResourceRef` so callers do not need to know which
table backs a resource. For books it also reads the existing ``books`` table to
find the personal owner — the one place this subsystem reaches into the core
schema, and it only *reads*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.workspaces.policy import Decision, GrantSet, allowed_actions, decide
from app.workspaces.repositories import (
    MemberRepo,
    OrganizationRepo,
    ResourceShareRepo,
    WorkspaceBookRepo,
    WorkspaceRepo,
)
from app.workspaces.roles import Action, ResourceType, Role, max_role


class AuthorizationError(Exception):
    """Raised by :meth:`AuthorizationService.require` when access is denied.

    Carries the :class:`~app.workspaces.policy.Decision` so the API layer can
    render a typed 403 with the human-readable reason and the user's effective
    role.
    """

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A reference to a resource the engine can authorize against.

    ``resource_type`` selects how grants are resolved; ``resource_id`` is the row
    id. For a book this is the ``books.id``; for a workspace/collection it is that
    row's id.
    """

    type: ResourceType
    id: str

    @classmethod
    def book(cls, book_id: str) -> ResourceRef:
        return cls(ResourceType.BOOK, book_id)

    @classmethod
    def workspace(cls, workspace_id: str) -> ResourceRef:
        return cls(ResourceType.WORKSPACE, workspace_id)

    @classmethod
    def collection(cls, collection_id: str) -> ResourceRef:
        return cls(ResourceType.COLLECTION, collection_id)


class AuthorizationService:
    """Resolve grants + decide — the ``can(user, action, resource)`` engine.

    Bind one to a request :class:`AsyncSession`. Reads only; it never mutates a
    row, so it is safe to call from any path (including read endpoints).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._members = MemberRepo(session)
        self._shares = ResourceShareRepo(session)
        self._workspace_books = WorkspaceBookRepo(session)
        self._workspaces = WorkspaceRepo(session)
        self._orgs = OrganizationRepo(session)

    # -- the public surface -------------------------------------------------- #

    async def can(self, user_id: str, action: Action, resource: ResourceRef) -> bool:
        """True iff ``user_id`` may perform ``action`` on ``resource``."""
        decision = await self.decide(user_id, action, resource)
        return decision.allowed

    async def decide(self, user_id: str, action: Action, resource: ResourceRef) -> Decision:
        """The full :class:`Decision` (allow/deny + effective role + reason)."""
        grants = await self.resolve_grants(user_id, resource)
        return decide(action, grants)

    async def require(self, user_id: str, action: Action, resource: ResourceRef) -> Decision:
        """Like :meth:`decide` but raise :class:`AuthorizationError` on deny."""
        decision = await self.decide(user_id, action, resource)
        if not decision.allowed:
            raise AuthorizationError(decision)
        return decision

    async def effective_role(self, user_id: str, resource: ResourceRef) -> Role | None:
        """The single strongest role ``user_id`` holds on ``resource`` (or ``None``)."""
        grants = await self.resolve_grants(user_id, resource)
        return grants.effective_role()

    async def allowed_actions(self, user_id: str, resource: ResourceRef) -> frozenset[Action]:
        """Every action ``user_id`` may take on ``resource`` (for UI capability hints)."""
        grants = await self.resolve_grants(user_id, resource)
        return allowed_actions(grants)

    # -- grant resolution ---------------------------------------------------- #

    async def resolve_grants(self, user_id: str, resource: ResourceRef) -> GrantSet:
        """Resolve every live grant ``user_id`` holds on ``resource``, by path."""
        now = datetime.now(UTC)
        direct = await self._direct_share_role(user_id, resource, now=now)

        if resource.type == ResourceType.WORKSPACE:
            ws_role = await self._workspace_membership_role(user_id, resource.id)
            org_role = await self._org_owner_role_for_workspace(user_id, resource.id)
            return GrantSet(direct_share=direct, workspace_role=ws_role, org_owner=org_role)

        if resource.type == ResourceType.BOOK:
            personal = await self._book_personal_owner_role(user_id, resource.id)
            ws_role, org_role = await self._book_workspace_roles(user_id, resource.id)
            return GrantSet(
                personal_owner=personal,
                direct_share=direct,
                workspace_role=ws_role,
                org_owner=org_role,
            )

        if resource.type == ResourceType.COLLECTION:
            ws_role, org_role = await self._collection_workspace_roles(user_id, resource.id)
            return GrantSet(direct_share=direct, workspace_role=ws_role, org_owner=org_role)

        return GrantSet(direct_share=direct)  # pragma: no cover - exhaustive above

    # -- per-path resolvers -------------------------------------------------- #

    async def _direct_share_role(
        self, user_id: str, resource: ResourceRef, *, now: datetime
    ) -> Role | None:
        share = await self._shares.get_live(resource.type, resource.id, user_id, now=now)
        return share.role if share is not None else None

    async def _book_personal_owner_role(self, user_id: str, book_id: str) -> Role | None:
        """The durable ``books.user_id`` owner is OWNER of their own book."""
        book = await self.session.get(Book, book_id)
        if book is not None and book.user_id is not None and book.user_id == user_id:
            return Role.OWNER
        return None

    async def _workspace_membership_role(self, user_id: str, workspace_id: str) -> Role | None:
        member = await self._members.get_active(workspace_id, user_id)
        return member.role if member is not None else None

    async def _org_owner_role_for_workspace(
        self, user_id: str, workspace_id: str
    ) -> Role | None:
        ws = await self._workspaces.get(workspace_id)
        if ws is None:
            return None
        org = await self._orgs.get(ws.org_id)
        if org is not None and org.owner_user_id == user_id:
            return Role.OWNER
        return None

    async def _book_workspace_roles(
        self, user_id: str, book_id: str
    ) -> tuple[Role | None, Role | None]:
        """The strongest workspace + org-owner role for any workspace holding the book."""
        workspace_ids = await self._workspace_books.workspaces_for_book(book_id)
        ws_roles: list[Role | None] = []
        org_roles: list[Role | None] = []
        for ws_id in workspace_ids:
            ws_roles.append(await self._workspace_membership_role(user_id, ws_id))
            org_roles.append(await self._org_owner_role_for_workspace(user_id, ws_id))
        return max_role(*ws_roles), max_role(*org_roles)

    async def _collection_workspace_roles(
        self, user_id: str, collection_id: str
    ) -> tuple[Role | None, Role | None]:
        from app.workspaces.repositories import CollectionRepo

        coll = await CollectionRepo(self.session).get(collection_id)
        if coll is None:
            return None, None
        ws_role = await self._workspace_membership_role(user_id, coll.workspace_id)
        org_role = await self._org_owner_role_for_workspace(user_id, coll.workspace_id)
        return ws_role, org_role


__all__ = [
    "AuthorizationError",
    "AuthorizationService",
    "ResourceRef",
]
