"""Multi-tenant isolation + org/workspace/membership model (kinora.md §6, §11.1).

A platform layer *on top of* the existing single-user identity (``users``) and
the global video-seconds ceiling (:class:`app.memory.budget_service.BudgetService`).
It makes Kinora multi-tenant along four axes — **data, spend, quota, assets** —
without touching any existing table or contract:

* **domain** (:mod:`.domain`) — an org → workspaces → members model with the four
  canonical roles (owner/admin/editor/viewer) plus repository protocols and
  in-memory fakes.
* **context** (:mod:`.context`) — a per-request :class:`TenantContext` resolved
  once and propagated task-locally via ``contextvars`` (no signature threading).
* **guard** (:mod:`.guard`) — a ``tenant_scoped`` query guard that builds the
  tenant filter for you and *raises* on any scoped query that lacks one, making
  cross-tenant data access structurally impossible.
* **quota** (:mod:`.quota`) — per-tenant book/USD/video-second envelopes that
  *compose* with the global ceiling (the binding cap is always the smaller).
* **assets** (:mod:`.assets`) — per-tenant object-store prefix isolation + config
  overrides.
* **roles**/**policy** (:mod:`.roles`, :mod:`.policy`) — the role lattice and the
  fail-closed ``require(permission, ctx)`` check.
* **service** (:mod:`.service`) — :class:`TenancyService`, the façade wiring it
  together.

Everything is pure logic over repository protocols, so the whole subsystem is
exhaustively unit-testable with no DB and no network.
"""

from __future__ import annotations

from app.tenancy.assets import (
    CrossTenantAssetError,
    TenantConfig,
    assert_in_tenant,
    belongs_to_tenant,
    derive_prefix,
    listing_prefix,
    scoped_key,
    tenant_prefix,
)
from app.tenancy.context import (
    NoTenantContext,
    TenantContext,
    TenantKind,
    TenantRef,
    bind_tenant,
    current_tenant,
    current_tenant_key,
    require_tenant,
    reset_tenant,
    use_tenant,
)
from app.tenancy.domain import (
    InMemoryMembershipRepo,
    InMemoryOrgRepo,
    InMemoryQuotaRepo,
    InMemoryTenancyStore,
    InMemoryWorkspaceRepo,
    Membership,
    MembershipRepo,
    Organization,
    OrgRepo,
    QuotaRepo,
    Workspace,
    WorkspaceRepo,
)
from app.tenancy.guard import (
    MissingTenantColumnError,
    UnscopedQueryError,
    assert_scoped,
    guard_select,
    tenant_column_name,
    tenant_filter,
    tenant_scoped,
)
from app.tenancy.policy import (
    PermissionDenied,
    check,
    require,
    require_any,
)
from app.tenancy.quota import (
    GlobalState,
    QuotaDecision,
    QuotaEnvelope,
    QuotaExceeded,
    QuotaResource,
    QuotaScope,
    Usage,
    evaluate,
    reserve,
)
from app.tenancy.roles import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    effective_role,
    has_capability,
    role_at_least,
    role_grants,
    role_permissions,
    role_rank,
)
from app.tenancy.service import (
    GlobalRemainingProvider,
    TenancyService,
    compose_envelope,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "CrossTenantAssetError",
    "GlobalRemainingProvider",
    "GlobalState",
    "InMemoryMembershipRepo",
    "InMemoryOrgRepo",
    "InMemoryQuotaRepo",
    "InMemoryTenancyStore",
    "InMemoryWorkspaceRepo",
    "Membership",
    "MembershipRepo",
    "MissingTenantColumnError",
    "NoTenantContext",
    "OrgRepo",
    "Organization",
    "Permission",
    "PermissionDenied",
    "QuotaDecision",
    "QuotaEnvelope",
    "QuotaExceeded",
    "QuotaRepo",
    "QuotaResource",
    "QuotaScope",
    "Role",
    "TenancyService",
    "TenantConfig",
    "TenantContext",
    "TenantKind",
    "TenantRef",
    "UnscopedQueryError",
    "Usage",
    "Workspace",
    "WorkspaceRepo",
    "assert_in_tenant",
    "assert_scoped",
    "belongs_to_tenant",
    "bind_tenant",
    "check",
    "compose_envelope",
    "current_tenant",
    "current_tenant_key",
    "derive_prefix",
    "effective_role",
    "evaluate",
    "guard_select",
    "has_capability",
    "listing_prefix",
    "require",
    "require_any",
    "require_tenant",
    "reset_tenant",
    "reserve",
    "role_at_least",
    "role_grants",
    "role_permissions",
    "role_rank",
    "scoped_key",
    "tenant_column_name",
    "tenant_filter",
    "tenant_prefix",
    "tenant_scoped",
    "use_tenant",
]
