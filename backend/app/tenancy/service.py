"""The orchestrating service: resolve context, compose envelopes, enforce quota.

:class:`TenancyService` is the stateful façade over the pure pieces. It:

* **resolves** a :class:`~app.tenancy.context.TenantContext` for a (user, tenant)
  pair — looking up the user's org + workspace memberships, collapsing them to an
  effective role, and attaching the tenant's asset prefix + config overrides;
* **composes** the effective quota envelope (a workspace envelope, if set,
  *tightens* the org envelope per-resource — the binding cap is always the
  smaller) and enforces a charge against it + the global ceiling via
  :func:`app.tenancy.quota.reserve`; and
* exposes thin pass-throughs to the permission check and the asset scoper so a
  caller has one object to depend on.

It depends only on the repository :class:`Protocol`\\s, so the in-memory fakes
drive it in tests and SQLAlchemy adapters drive it in production. A global-ceiling
provider is injected as a plain callable (a local ``GlobalRemainingProvider``
protocol) so this layer never imports the budget service — keeping the dependency
arrow pointing the right way and the tests infra-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog

from app.tenancy.assets import derive_prefix, scoped_key
from app.tenancy.context import (
    TenantContext,
    TenantKind,
    TenantRef,
    use_tenant,
)
from app.tenancy.domain import (
    MembershipRepo,
    Organization,
    OrgRepo,
    QuotaRepo,
    Workspace,
    WorkspaceRepo,
)
from app.tenancy.policy import require as policy_require
from app.tenancy.quota import (
    GlobalState,
    QuotaDecision,
    QuotaEnvelope,
    QuotaResource,
    reserve,
)
from app.tenancy.roles import Permission, Role, effective_role

log = structlog.get_logger(__name__)

#: A callable returning the global video-seconds head-room (``inf`` == unbounded).
#: A local seam so the service composes with §11.1 without importing it.
GlobalRemainingProvider = Callable[[], float]


def _unbounded_global() -> float:
    return float("inf")


def compose_envelope(org: Organization, workspace: Workspace | None) -> QuotaEnvelope:
    """The effective envelope: a workspace cap *tightens* the org cap per-resource.

    For each resource the binding cap is the smaller of the org cap and the
    workspace cap, treating ``0`` (unlimited-by-envelope) as +∞ so it never
    *loosens* the other envelope.
    """
    base = org.envelope
    if workspace is None or workspace.envelope is None:
        return base
    ws = workspace.envelope

    def tighter(a: float, b: float) -> float:
        # 0 == unlimited; only a positive cap constrains.
        if a == 0:
            return b
        if b == 0:
            return a
        return min(a, b)

    return QuotaEnvelope(
        max_books=int(tighter(float(base.max_books), float(ws.max_books))),
        monthly_usd=tighter(base.monthly_usd, ws.monthly_usd),
        monthly_video_seconds=tighter(
            base.monthly_video_seconds, ws.monthly_video_seconds
        ),
    )


class TenancyService:
    """Wire the tenancy repositories, context, RBAC, quota, and assets together."""

    def __init__(
        self,
        *,
        orgs: OrgRepo,
        workspaces: WorkspaceRepo,
        memberships: MembershipRepo,
        quotas: QuotaRepo,
        global_remaining: GlobalRemainingProvider | None = None,
    ) -> None:
        self._orgs = orgs
        self._workspaces = workspaces
        self._memberships = memberships
        self._quotas = quotas
        self._global_remaining = global_remaining or _unbounded_global

    # -- context resolution ------------------------------------------------- #

    def resolve_context(
        self,
        *,
        user_id: str,
        org_id: str,
        workspace_id: str | None = None,
        extra_capabilities: frozenset[str] = frozenset(),
    ) -> TenantContext | None:
        """Build the :class:`TenantContext` for a user acting in a tenant.

        Returns ``None`` (fail-closed) if the org doesn't exist or the user holds
        no active membership reaching the requested tenant. The effective role is
        the strongest of the user's org-level and workspace-level memberships.
        """
        org = self._orgs.get(org_id)
        if org is None:
            return None

        org_membership = self._memberships.org_membership(user_id, org_id)
        org_role = org_membership.role if org_membership is not None else None

        workspace: Workspace | None = None
        ws_role: Role | None = None
        if workspace_id is not None:
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or workspace.org_id != org_id:
                return None
            ws_membership = self._memberships.workspace_membership(user_id, workspace_id)
            ws_role = ws_membership.role if ws_membership is not None else None

        role = effective_role(org_role, ws_role)
        if role is None:
            # No membership reaching this tenant: deny.
            return None

        if workspace is not None:
            tenant = TenantRef.workspace(workspace.id, org_id=org_id)
            overrides = {**dict(org.config_overrides), **dict(workspace.config_overrides)}
        else:
            tenant = TenantRef.organization(org_id)
            overrides = dict(org.config_overrides)

        return TenantContext(
            tenant=tenant,
            principal_id=user_id,
            role=role,
            extra_capabilities=extra_capabilities,
            asset_prefix=derive_prefix(tenant.key),
            config_overrides=overrides,
        )

    def scope(self, ctx: TenantContext) -> Any:
        """A ``with`` scope binding ``ctx`` to the current task (sugar)."""
        return use_tenant(ctx)

    # -- RBAC pass-through --------------------------------------------------- #

    def require(
        self, permission: Permission, ctx: TenantContext, *, within: str | None = None
    ) -> TenantContext:
        """Assert ``ctx`` grants ``permission`` (delegates to the policy)."""
        return policy_require(permission, ctx, within=within)

    # -- quota composition + enforcement ------------------------------------ #

    def effective_envelope(self, ctx: TenantContext) -> QuotaEnvelope:
        """The composed (org ∧ workspace) envelope for the context's tenant."""
        org = self._orgs.get(ctx.org_id)
        if org is None:
            return QuotaEnvelope()
        workspace = None
        if ctx.tenant.kind is TenantKind.WORKSPACE:
            workspace = self._workspaces.get(ctx.tenant.tenant_id)
        return compose_envelope(org, workspace)

    def _global_state(self, resource: QuotaResource) -> GlobalState:
        if resource is QuotaResource.VIDEO_SECONDS:
            return GlobalState(video_seconds_remaining=self._global_remaining())
        return GlobalState()

    def check_quota(
        self, ctx: TenantContext, resource: QuotaResource, amount: float
    ) -> QuotaDecision:
        """Enforce a charge against the composed envelope + global ceiling.

        Raises :class:`~app.tenancy.quota.QuotaExceeded` when denied; returns the
        allowing :class:`QuotaDecision` otherwise. Does **not** record usage — the
        caller commits via :meth:`record_usage` after the resource is actually
        consumed (mirroring the budget service's reserve/commit split).
        """
        envelope = self.effective_envelope(ctx)
        usage = self._quotas.usage(ctx.tenant_key)
        return reserve(
            resource,
            amount,
            envelope=envelope,
            usage=usage,
            global_state=self._global_state(resource),
        )

    def record_usage(
        self, ctx: TenantContext, resource: QuotaResource, amount: float
    ) -> None:
        """Commit ``amount`` of ``resource`` to the tenant's usage ledger."""
        self._quotas.record(ctx.tenant_key, resource, amount)

    # -- asset scoping pass-through ----------------------------------------- #

    def asset_key(self, ctx: TenantContext, key: str) -> str:
        """Scope a global object ``key`` onto the tenant's prefix."""
        return scoped_key(key, ctx)

    # -- config overrides --------------------------------------------------- #

    def config_value(self, ctx: TenantContext, key: str, defaults: Mapping[str, Any]) -> Any:
        """The tenant override for ``key`` else the global ``defaults`` value."""
        if key in ctx.config_overrides:
            return ctx.config_overrides[key]
        return defaults.get(key)


__all__ = [
    "GlobalRemainingProvider",
    "TenancyService",
    "compose_envelope",
]
