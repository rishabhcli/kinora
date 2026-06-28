"""Workspaces & teams REST surface (kinora.md §5 — collaboration ownership).

The HTTP face of :mod:`app.workspaces`: organizations + seats, workspaces + their
settings/quotas, members + email-token invitations, role-based sharing of books +
collections, transfer-of-ownership, an access probe (``can``), and the activity
feed. Every mutating verb is authed (Bearer JWT → :data:`CurrentUser`) and runs
through the workspace service, which enforces the
:class:`~app.workspaces.authz.AuthorizationService` policy + quotas and emits an
activity row.

Composition note: this router is *additive*. It builds a
:class:`~app.workspaces.service.WorkspaceService` per request from the container's
``session_factory`` and ``settings.jwt_secret`` (reused as the invitation-signing
secret), so it needs no edit to ``composition.py`` — only the additive registration
in ``app/api/routes/__init__.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.composition import Container
from app.core.logging import get_logger
from app.workspaces.authz import AuthorizationError, ResourceRef
from app.workspaces.invitations import InvitationTokenError
from app.workspaces.quotas import QuotaExceeded
from app.workspaces.roles import Action, ResourceType
from app.workspaces.schemas import (
    AcceptInvitationRequest,
    AccessResponse,
    ActivityResponse,
    AddMemberRequest,
    AttachBookRequest,
    ChangeRoleRequest,
    CollectionItemRequest,
    CollectionResponse,
    CreateCollectionRequest,
    CreateWorkspaceRequest,
    DecisionResponse,
    InvitationResponse,
    InviteRequest,
    MemberResponse,
    OkResponse,
    SeatUsageResponse,
    SetSeatsRequest,
    ShareRequest,
    ShareResponse,
    TransferRequest,
    TransferResponse,
    TransferResponseRequest,
    UpdateSettingsRequest,
    WorkspaceResponse,
)
from app.workspaces.service import WorkspaceError, WorkspaceService

logger = get_logger("app.api.workspaces")

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

WriteGuard = Annotated[None, Depends(write_rate_limit)]


@asynccontextmanager
async def _service(container: Container) -> AsyncIterator[WorkspaceService]:
    """Open a committing unit of work and yield a bound :class:`WorkspaceService`.

    Mirrors the container's ``session_factory`` boundary: commit on clean exit,
    roll back on error. Service operations only ``flush``; this context owns the
    transaction.
    """
    async with container.session_factory() as session:
        yield WorkspaceService(session, invite_secret=container.settings.jwt_secret)


def _map_error(exc: Exception) -> APIError:
    """Translate a domain error into the gateway's typed :class:`APIError`."""
    if isinstance(exc, AuthorizationError):
        d = exc.decision
        return APIError(
            "forbidden",
            d.reason,
            status=403,
            detail={
                "action": d.action.value,
                "effective_role": d.effective_role.value if d.effective_role else None,
            },
        )
    if isinstance(exc, QuotaExceeded):
        return APIError(
            "quota_exceeded",
            str(exc),
            status=409,
            detail={"quota": exc.quota, "limit": exc.limit, "used": exc.used},
        )
    if isinstance(exc, InvitationTokenError):
        return APIError("invitation_invalid", str(exc), status=400)
    if isinstance(exc, WorkspaceError):
        return APIError(exc.code, exc.message, status=exc.status)
    raise exc  # pragma: no cover - unexpected; let the global handler scrub it


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: CreateWorkspaceRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> WorkspaceResponse:
    """Create a workspace; the caller becomes its OWNER (mints an org if needed)."""
    try:
        async with _service(container) as svc:
            ws = await svc.create_workspace(
                owner_user_id=user.id,
                name=body.name,
                org_id=body.org_id,
                description=body.description,
                settings=body.settings,
            )
            role = await svc.authz.effective_role(user.id, ResourceRef.workspace(ws.id))
            return WorkspaceResponse.of(ws, my_role=role)
    except Exception as exc:  # noqa: BLE001 - mapped to a typed APIError
        raise _map_error(exc) from exc


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(container: ContainerDep, user: CurrentUser) -> list[WorkspaceResponse]:
    """Every workspace the caller can reach (membership + orgs they own)."""
    async with _service(container) as svc:
        out: list[WorkspaceResponse] = []
        for ws in await svc.list_workspaces_for_user(user.id):
            role = await svc.authz.effective_role(user.id, ResourceRef.workspace(ws.id))
            out.append(WorkspaceResponse.of(ws, my_role=role))
        return out


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str, container: ContainerDep, user: CurrentUser
) -> WorkspaceResponse:
    """Fetch one workspace (requires VIEW access)."""
    try:
        async with _service(container) as svc:
            ws = await svc._require_workspace(workspace_id)
            await svc.authz.require(user.id, Action.VIEW, ResourceRef.workspace(workspace_id))
            role = await svc.authz.effective_role(user.id, ResourceRef.workspace(workspace_id))
            return WorkspaceResponse.of(ws, my_role=role)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.patch("/{workspace_id}/settings", response_model=WorkspaceResponse)
async def update_settings(
    workspace_id: str,
    body: UpdateSettingsRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> WorkspaceResponse:
    """Replace a workspace's settings/quotas bag (requires MANAGE_SETTINGS)."""
    try:
        async with _service(container) as svc:
            ws = await svc.update_workspace_settings(
                actor_user_id=user.id, workspace_id=workspace_id, settings=body.settings
            )
            return WorkspaceResponse.of(ws)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/{workspace_id}", response_model=OkResponse)
async def delete_workspace(
    workspace_id: str, container: ContainerDep, user: CurrentUser, _rl: WriteGuard
) -> OkResponse:
    """Destroy a workspace and its edges (requires DELETE)."""
    try:
        async with _service(container) as svc:
            await svc.delete_workspace(actor_user_id=user.id, workspace_id=workspace_id)
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Members + invitations
# --------------------------------------------------------------------------- #


@router.get("/{workspace_id}/members", response_model=list[MemberResponse])
async def list_members(
    workspace_id: str, container: ContainerDep, user: CurrentUser
) -> list[MemberResponse]:
    """List active members (requires VIEW)."""
    try:
        async with _service(container) as svc:
            await svc._require_workspace(workspace_id)
            await svc.authz.require(user.id, Action.VIEW, ResourceRef.workspace(workspace_id))
            members = await svc.list_members(workspace_id)
            return [MemberResponse.of(m) for m in members]
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post(
    "/{workspace_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED
)
async def add_member(
    workspace_id: str,
    body: AddMemberRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> MemberResponse:
    """Directly add an existing user to a workspace (requires MANAGE_MEMBERS)."""
    try:
        async with _service(container) as svc:
            member = await svc.add_member(
                actor_user_id=user.id,
                workspace_id=workspace_id,
                user_id=body.user_id,
                role=body.role,
            )
            return MemberResponse.of(member)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.patch("/{workspace_id}/members/{member_user_id}", response_model=MemberResponse)
async def change_member_role(
    workspace_id: str,
    member_user_id: str,
    body: ChangeRoleRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> MemberResponse:
    """Change a member's role (requires MANAGE_MEMBERS)."""
    try:
        async with _service(container) as svc:
            member = await svc.change_member_role(
                actor_user_id=user.id,
                workspace_id=workspace_id,
                user_id=member_user_id,
                role=body.role,
            )
            return MemberResponse.of(member)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/{workspace_id}/members/{member_user_id}", response_model=OkResponse)
async def remove_member(
    workspace_id: str,
    member_user_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Soft-remove a member (requires MANAGE_MEMBERS; org owner cannot be removed)."""
    try:
        async with _service(container) as svc:
            await svc.remove_member(
                actor_user_id=user.id, workspace_id=workspace_id, user_id=member_user_id
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post(
    "/{workspace_id}/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    workspace_id: str,
    body: InviteRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> InvitationResponse:
    """Issue a signed email-token invitation (requires MANAGE_MEMBERS).

    The token is returned **only here** (the apps email/show it once); it is never
    echoed back on subsequent reads.
    """
    try:
        async with _service(container) as svc:
            result = await svc.invite_member(
                actor_user_id=user.id,
                workspace_id=workspace_id,
                email=body.email,
                role=body.role,
            )
            return InvitationResponse.of(result.invitation, token=result.token)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.get("/{workspace_id}/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    workspace_id: str, container: ContainerDep, user: CurrentUser
) -> list[InvitationResponse]:
    """Pending invitations for a workspace (requires MANAGE_MEMBERS)."""
    try:
        async with _service(container) as svc:
            await svc._require_workspace(workspace_id)
            await svc.authz.require(
                user.id, Action.MANAGE_MEMBERS, ResourceRef.workspace(workspace_id)
            )
            invitations = await svc.list_pending_invitations(workspace_id)
            return [InvitationResponse.of(i) for i in invitations]
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/invitations/{invitation_id}", response_model=OkResponse)
async def revoke_invitation(
    invitation_id: str, container: ContainerDep, user: CurrentUser, _rl: WriteGuard
) -> OkResponse:
    """Revoke a pending invitation (requires MANAGE_MEMBERS on its workspace)."""
    try:
        async with _service(container) as svc:
            await svc.revoke_invitation(actor_user_id=user.id, invitation_id=invitation_id)
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/invitations/accept", response_model=MemberResponse)
async def accept_invitation(
    body: AcceptInvitationRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> MemberResponse:
    """Accept an invitation token, joining the caller to the workspace."""
    try:
        async with _service(container) as svc:
            member = await svc.accept_invitation(token=body.token, accepting_user_id=user.id)
            return MemberResponse.of(member)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Seats
# --------------------------------------------------------------------------- #


@router.get("/orgs/{org_id}/seats", response_model=SeatUsageResponse)
async def seat_usage(
    org_id: str, container: ContainerDep, user: CurrentUser
) -> SeatUsageResponse:
    """An org's seat-consumption snapshot (org owner only)."""
    try:
        async with _service(container) as svc:
            await svc._require_org_owner(user.id, org_id)
            usage = await svc.seat_usage(org_id)
            return SeatUsageResponse(
                seats=usage.seats,
                active_members=usage.active_members,
                available=usage.available,
                unlimited=usage.unlimited,
            )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.patch("/orgs/{org_id}/seats", response_model=SeatUsageResponse)
async def set_seats(
    org_id: str,
    body: SetSeatsRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> SeatUsageResponse:
    """Adjust an org's purchased seats (org owner only)."""
    try:
        async with _service(container) as svc:
            await svc.set_seats(actor_user_id=user.id, org_id=org_id, seats=body.seats)
            usage = await svc.seat_usage(org_id)
            return SeatUsageResponse(
                seats=usage.seats,
                active_members=usage.active_members,
                available=usage.available,
                unlimited=usage.unlimited,
            )
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Shared library — books + direct shares
# --------------------------------------------------------------------------- #


@router.post(
    "/{workspace_id}/books",
    response_model=OkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def attach_book(
    workspace_id: str,
    body: AttachBookRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Attach a book to a workspace's shared shelf (SHARE on the book + quota)."""
    try:
        async with _service(container) as svc:
            await svc.attach_book(
                actor_user_id=user.id, workspace_id=workspace_id, book_id=body.book_id
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/{workspace_id}/books/{book_id}", response_model=OkResponse)
async def detach_book(
    workspace_id: str,
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Detach a book from a workspace (requires MANAGE_COLLECTIONS)."""
    try:
        async with _service(container) as svc:
            await svc.detach_book(
                actor_user_id=user.id, workspace_id=workspace_id, book_id=book_id
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/books/{book_id}/shares", response_model=ShareResponse)
async def share_book(
    book_id: str,
    body: ShareRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> ShareResponse:
    """Grant a direct role on a book to another user (by id or email)."""
    try:
        async with _service(container) as svc:
            resource = ResourceRef.book(book_id)
            if body.user_id:
                share = await svc.share_resource(
                    actor_user_id=user.id,
                    resource=resource,
                    grantee_user_id=body.user_id,
                    role=body.role,
                    expires_at=body.expires_at,
                )
            elif body.email:
                share = await svc.share_resource_by_email(
                    actor_user_id=user.id, resource=resource, email=body.email, role=body.role
                )
            else:
                raise WorkspaceError("missing_grantee", "provide user_id or email")
            return ShareResponse.of(share)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/books/{book_id}/shares/{grantee_user_id}", response_model=OkResponse)
async def revoke_book_share(
    book_id: str,
    grantee_user_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Revoke a direct book share (requires SHARE on the book)."""
    try:
        async with _service(container) as svc:
            await svc.revoke_share(
                actor_user_id=user.id,
                resource=ResourceRef.book(book_id),
                grantee_user_id=grantee_user_id,
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.get("/me/books", response_model=list[str])
async def my_accessible_books(container: ContainerDep, user: CurrentUser) -> list[str]:
    """Book ids the caller can reach via any path (shares + workspace shelves)."""
    async with _service(container) as svc:
        return await svc.list_shared_books_for_user(user.id)


@router.post("/shares/{resource_type}/{resource_id}", response_model=ShareResponse)
async def share_resource(
    resource_type: ResourceType,
    resource_id: str,
    body: ShareRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> ShareResponse:
    """Grant a direct role on any resource (book/collection/workspace) to a user."""
    try:
        async with _service(container) as svc:
            resource = ResourceRef(resource_type, resource_id)
            if body.user_id:
                share = await svc.share_resource(
                    actor_user_id=user.id,
                    resource=resource,
                    grantee_user_id=body.user_id,
                    role=body.role,
                    expires_at=body.expires_at,
                )
            elif body.email:
                share = await svc.share_resource_by_email(
                    actor_user_id=user.id, resource=resource, email=body.email, role=body.role
                )
            else:
                raise WorkspaceError("missing_grantee", "provide user_id or email")
            return ShareResponse.of(share)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.get("/shares/{resource_type}/{resource_id}", response_model=list[ShareResponse])
async def list_resource_shares(
    resource_type: ResourceType,
    resource_id: str,
    container: ContainerDep,
    user: CurrentUser,
) -> list[ShareResponse]:
    """List the direct shares on a resource (requires SHARE / owner-level access)."""
    try:
        async with _service(container) as svc:
            shares = await svc.list_resource_shares(
                actor_user_id=user.id, resource=ResourceRef(resource_type, resource_id)
            )
            return [ShareResponse.of(s) for s in shares]
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete(
    "/shares/{resource_type}/{resource_id}/{grantee_user_id}", response_model=OkResponse
)
async def revoke_resource_share(
    resource_type: ResourceType,
    resource_id: str,
    grantee_user_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Revoke a direct share on any resource (requires SHARE)."""
    try:
        async with _service(container) as svc:
            await svc.revoke_share(
                actor_user_id=user.id,
                resource=ResourceRef(resource_type, resource_id),
                grantee_user_id=grantee_user_id,
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


@router.post(
    "/{workspace_id}/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    workspace_id: str,
    body: CreateCollectionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> CollectionResponse:
    """Create a named collection in a workspace (requires MANAGE_COLLECTIONS)."""
    try:
        async with _service(container) as svc:
            coll = await svc.create_collection(
                actor_user_id=user.id,
                workspace_id=workspace_id,
                name=body.name,
                description=body.description,
            )
            return CollectionResponse.of(coll)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/collections/{collection_id}/items", response_model=OkResponse)
async def add_collection_item(
    collection_id: str,
    body: CollectionItemRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Add a book to a collection (requires MANAGE_COLLECTIONS)."""
    try:
        async with _service(container) as svc:
            await svc.add_to_collection(
                actor_user_id=user.id, collection_id=collection_id, book_id=body.book_id
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.delete("/collections/{collection_id}/items/{book_id}", response_model=OkResponse)
async def remove_collection_item(
    collection_id: str,
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> OkResponse:
    """Remove a book from a collection (requires MANAGE_COLLECTIONS)."""
    try:
        async with _service(container) as svc:
            await svc.remove_from_collection(
                actor_user_id=user.id, collection_id=collection_id, book_id=book_id
            )
            return OkResponse()
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Transfer of ownership
# --------------------------------------------------------------------------- #


@router.post("/books/{book_id}/transfer", response_model=TransferResponse)
async def request_book_transfer(
    book_id: str,
    body: TransferRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> TransferResponse:
    """Open a transfer of a book's ownership (requires TRANSFER_OWNERSHIP)."""
    try:
        async with _service(container) as svc:
            transfer = await svc.request_transfer(
                actor_user_id=user.id,
                resource=ResourceRef.book(book_id),
                to_user_id=body.to_user_id,
                note=body.note,
            )
            return TransferResponse.of(transfer)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/transfers/{transfer_id}/respond", response_model=TransferResponse)
async def respond_to_transfer(
    transfer_id: str,
    body: TransferResponseRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: WriteGuard,
) -> TransferResponse:
    """The recipient accepts/declines a pending transfer (recipient only)."""
    try:
        async with _service(container) as svc:
            transfer = await svc.respond_to_transfer(
                actor_user_id=user.id, transfer_id=transfer_id, accept=body.accept
            )
            return TransferResponse.of(transfer)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferResponse)
async def cancel_transfer(
    transfer_id: str, container: ContainerDep, user: CurrentUser, _rl: WriteGuard
) -> TransferResponse:
    """The requester cancels a pending transfer (requester only)."""
    try:
        async with _service(container) as svc:
            transfer = await svc.cancel_transfer(actor_user_id=user.id, transfer_id=transfer_id)
            return TransferResponse.of(transfer)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


# --------------------------------------------------------------------------- #
# Access probe + activity feed
# --------------------------------------------------------------------------- #


@router.get("/access/{resource_type}/{resource_id}", response_model=AccessResponse)
async def probe_access(
    resource_type: ResourceType,
    resource_id: str,
    container: ContainerDep,
    user: CurrentUser,
) -> AccessResponse:
    """The caller's effective role + allowed actions on a resource (the ``can`` probe)."""
    async with _service(container) as svc:
        ref = ResourceRef(resource_type, resource_id)
        role = await svc.authz.effective_role(user.id, ref)
        actions = await svc.authz.allowed_actions(user.id, ref)
        return AccessResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            effective_role=role,
            allowed_actions=sorted(actions, key=lambda a: a.value),
        )


@router.get("/access/{resource_type}/{resource_id}/{action}", response_model=DecisionResponse)
async def probe_action(
    resource_type: ResourceType,
    resource_id: str,
    action: Action,
    container: ContainerDep,
    user: CurrentUser,
) -> DecisionResponse:
    """A single allow/deny decision for one action (the ``can(user, action, resource)`` API)."""
    async with _service(container) as svc:
        decision = await svc.authz.decide(
            user.id, action, ResourceRef(resource_type, resource_id)
        )
        return DecisionResponse(
            allowed=decision.allowed,
            action=decision.action,
            effective_role=decision.effective_role,
            reason=decision.reason,
        )


@router.get("/{workspace_id}/activity", response_model=list[ActivityResponse])
async def activity_feed(
    workspace_id: str,
    container: ContainerDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ActivityResponse]:
    """A workspace's activity feed (requires VIEW_ACTIVITY)."""
    try:
        async with _service(container) as svc:
            rows = await svc.activity_feed(
                actor_user_id=user.id, workspace_id=workspace_id, limit=limit
            )
            return [ActivityResponse.of(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


__all__ = ["router"]
