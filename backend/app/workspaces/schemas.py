"""Transport DTOs for the workspaces REST surface.

Kept *inside* the workspaces package (rather than the shared ``app.api.schemas``)
so the subsystem is a self-contained additive unit and does not contend with other
agents editing the global schema module. These are the wire contracts the
``app/api/routes/workspaces.py`` router validates/projects; the ORM rows and the
service results stay internal.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.workspaces.models import (
    Collection,
    Organization,
    OwnershipTransfer,
    ResourceShare,
    Workspace,
    WorkspaceActivity,
    WorkspaceInvitation,
    WorkspaceMember,
)
from app.workspaces.roles import (
    Action,
    InvitationStatus,
    MemberStatus,
    OrgPlan,
    ResourceType,
    Role,
    TransferStatus,
)


def _email(value: str) -> str:
    value = value.strip().lower()
    if "@" not in value or "." not in value.split("@")[-1]:
        raise ValueError("invalid email address")
    return value


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class CreateWorkspaceRequest(BaseModel):
    """Create a workspace (mints a personal org if ``org_id`` is omitted)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    org_id: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    settings: dict | None = None


class UpdateSettingsRequest(BaseModel):
    """Replace a workspace's settings/quotas bag."""

    model_config = ConfigDict(extra="forbid")

    settings: dict = Field(default_factory=dict)


class InviteRequest(BaseModel):
    """Issue an email-token invitation to a workspace."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    role: Role = Role.VIEWER

    @field_validator("email")
    @classmethod
    def _v_email(cls, value: str) -> str:
        return _email(value)


class AcceptInvitationRequest(BaseModel):
    """Accept an invitation by its signed token."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=8, max_length=2048)


class AddMemberRequest(BaseModel):
    """Directly add an existing user to a workspace."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    role: Role = Role.VIEWER


class ChangeRoleRequest(BaseModel):
    """Change a member's role."""

    model_config = ConfigDict(extra="forbid")

    role: Role


class ShareRequest(BaseModel):
    """Share a resource directly with a user (by id or email)."""

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    email: str | None = None
    role: Role = Role.VIEWER
    expires_at: datetime | None = None

    @field_validator("email")
    @classmethod
    def _v_email(cls, value: str | None) -> str | None:
        return _email(value) if value else None


class AttachBookRequest(BaseModel):
    """Attach a book to a workspace's shared shelf."""

    model_config = ConfigDict(extra="forbid")

    book_id: str = Field(min_length=1, max_length=64)


class CreateCollectionRequest(BaseModel):
    """Create a named collection in a workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4000)


class CollectionItemRequest(BaseModel):
    """Add/remove a book to/from a collection."""

    model_config = ConfigDict(extra="forbid")

    book_id: str = Field(min_length=1, max_length=64)


class TransferRequest(BaseModel):
    """Open a transfer-of-ownership request."""

    model_config = ConfigDict(extra="forbid")

    to_user_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class TransferResponseRequest(BaseModel):
    """Accept or decline an incoming transfer."""

    model_config = ConfigDict(extra="forbid")

    accept: bool


class SetSeatsRequest(BaseModel):
    """Adjust an org's purchased seats."""

    model_config = ConfigDict(extra="forbid")

    seats: int = Field(ge=0, le=100_000)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class OrganizationResponse(BaseModel):
    """An organization projected for the wire."""

    id: str
    name: str
    owner_user_id: str | None
    plan: OrgPlan
    seats: int
    created_at: datetime

    @classmethod
    def of(cls, org: Organization) -> OrganizationResponse:
        return cls(
            id=org.id,
            name=org.name,
            owner_user_id=org.owner_user_id,
            plan=org.plan,
            seats=org.seats,
            created_at=org.created_at,
        )


class WorkspaceResponse(BaseModel):
    """A workspace projected for the wire."""

    id: str
    org_id: str
    name: str
    slug: str
    description: str | None
    settings: dict
    archived: bool
    created_at: datetime
    #: The caller's effective role on this workspace (filled by the route).
    my_role: Role | None = None

    @classmethod
    def of(cls, ws: Workspace, *, my_role: Role | None = None) -> WorkspaceResponse:
        return cls(
            id=ws.id,
            org_id=ws.org_id,
            name=ws.name,
            slug=ws.slug,
            description=ws.description,
            settings=ws.settings,
            archived=ws.archived,
            created_at=ws.created_at,
            my_role=my_role,
        )


class MemberResponse(BaseModel):
    """A workspace member projected for the wire."""

    id: str
    workspace_id: str
    user_id: str
    role: Role
    status: MemberStatus
    created_at: datetime

    @classmethod
    def of(cls, m: WorkspaceMember) -> MemberResponse:
        return cls(
            id=m.id,
            workspace_id=m.workspace_id,
            user_id=m.user_id,
            role=m.role,
            status=m.status,
            created_at=m.created_at,
        )


class InvitationResponse(BaseModel):
    """An invitation projected for the wire (token included only on creation)."""

    id: str
    workspace_id: str
    email: str
    role: Role
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime
    token: str | None = None

    @classmethod
    def of(
        cls, inv: WorkspaceInvitation, *, token: str | None = None
    ) -> InvitationResponse:
        return cls(
            id=inv.id,
            workspace_id=inv.workspace_id,
            email=inv.email,
            role=inv.role,
            status=inv.status,
            expires_at=inv.expires_at,
            created_at=inv.created_at,
            token=token,
        )


class ShareResponse(BaseModel):
    """A direct resource share projected for the wire."""

    id: str
    resource_type: ResourceType
    resource_id: str
    user_id: str
    role: Role
    expires_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, s: ResourceShare) -> ShareResponse:
        return cls(
            id=s.id,
            resource_type=s.resource_type,
            resource_id=s.resource_id,
            user_id=s.user_id,
            role=s.role,
            expires_at=s.expires_at,
            created_at=s.created_at,
        )


class CollectionResponse(BaseModel):
    """A collection projected for the wire."""

    id: str
    workspace_id: str
    name: str
    slug: str
    description: str | None
    created_at: datetime

    @classmethod
    def of(cls, c: Collection) -> CollectionResponse:
        return cls(
            id=c.id,
            workspace_id=c.workspace_id,
            name=c.name,
            slug=c.slug,
            description=c.description,
            created_at=c.created_at,
        )


class TransferResponse(BaseModel):
    """An ownership-transfer request projected for the wire."""

    id: str
    resource_type: ResourceType
    resource_id: str
    from_user_id: str | None
    to_user_id: str
    status: TransferStatus
    note: str | None
    created_at: datetime

    @classmethod
    def of(cls, t: OwnershipTransfer) -> TransferResponse:
        return cls(
            id=t.id,
            resource_type=t.resource_type,
            resource_id=t.resource_id,
            from_user_id=t.from_user_id,
            to_user_id=t.to_user_id,
            status=t.status,
            note=t.note,
            created_at=t.created_at,
        )


class ActivityResponse(BaseModel):
    """One activity-feed entry projected for the wire."""

    id: str
    workspace_id: str | None
    actor_user_id: str | None
    verb: str
    resource_type: ResourceType | None
    resource_id: str | None
    data: dict
    created_at: datetime

    @classmethod
    def of(cls, a: WorkspaceActivity) -> ActivityResponse:
        return cls(
            id=a.id,
            workspace_id=a.workspace_id,
            actor_user_id=a.actor_user_id,
            verb=a.verb,
            resource_type=a.resource_type,
            resource_id=a.resource_id,
            data=a.data,
            created_at=a.created_at,
        )


class SeatUsageResponse(BaseModel):
    """An org's seat-consumption snapshot."""

    seats: int
    active_members: int
    available: int
    unlimited: bool


class AccessResponse(BaseModel):
    """The caller's effective access on a resource (``can``-style probe)."""

    resource_type: ResourceType
    resource_id: str
    effective_role: Role | None
    allowed_actions: list[Action]


class DecisionResponse(BaseModel):
    """A single allow/deny decision for one action."""

    allowed: bool
    action: Action
    effective_role: Role | None
    reason: str


class OkResponse(BaseModel):
    """A trivial success envelope for verbs with no body to return."""

    ok: bool = True


__all__ = [
    "AcceptInvitationRequest",
    "AccessResponse",
    "ActivityResponse",
    "AddMemberRequest",
    "AttachBookRequest",
    "ChangeRoleRequest",
    "CollectionItemRequest",
    "CollectionResponse",
    "CreateCollectionRequest",
    "CreateWorkspaceRequest",
    "DecisionResponse",
    "InvitationResponse",
    "InviteRequest",
    "MemberResponse",
    "OkResponse",
    "OrganizationResponse",
    "SeatUsageResponse",
    "SetSeatsRequest",
    "ShareRequest",
    "ShareResponse",
    "TransferRequest",
    "TransferResponse",
    "TransferResponseRequest",
    "UpdateSettingsRequest",
    "WorkspaceResponse",
]
