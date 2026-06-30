"""Unit tests for the multi-tenant isolation layer (``app.tenancy``) — no infra.

Deterministic, network-free, KINORA_LIVE_VIDEO untouched. Covers:

* the role/permission matrix + the lattice superset invariant;
* the tenant-scoped query guard (builds the filter, blocks cross-tenant reads,
  raises on an unscoped query);
* per-tenant quota/spend envelope enforcement, composed with the global ceiling;
* asset-prefix isolation;
* ``contextvars`` context propagation + isolation across tasks;
* the ``TenancyService`` end-to-end wiring (context resolution + quota + RBAC).
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app import tenancy
from app.tenancy import (
    GlobalState,
    NoTenantContext,
    Permission,
    PermissionDenied,
    QuotaEnvelope,
    QuotaExceeded,
    QuotaResource,
    QuotaScope,
    Role,
    TenancyService,
    TenantContext,
    TenantRef,
    Usage,
)
from app.tenancy.context import use_tenant
from app.tenancy.domain import InMemoryTenancyStore
from app.tenancy.guard import (
    MissingTenantColumnError,
    UnscopedQueryError,
    assert_scoped,
    guard_select,
    tenant_scoped,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _ctx(
    tenant_id: str = "abc",
    *,
    role: Role | None = Role.EDITOR,
    org_id: str | None = None,
    kind: str = "org",
) -> TenantContext:
    if kind == "org":
        ref = TenantRef.organization(tenant_id)
    else:
        ref = TenantRef.workspace(tenant_id, org_id=org_id or "org1")
    return TenantContext(
        tenant=ref,
        principal_id="user1",
        role=role,
        asset_prefix=f"t/{ref.key.replace(':', '_')}",
    )


class _Base(DeclarativeBase):
    pass


class _Doc(_Base):
    """A throwaway tenant-owned ORM model for the guard tests."""

    __tablename__ = "tenancy_test_docs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(256))


class _Untenanted(_Base):
    """A model with no tenant column — the guard must refuse it."""

    __tablename__ = "tenancy_test_untenanted"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))


# --------------------------------------------------------------------------- #
# Roles / permission matrix
# --------------------------------------------------------------------------- #


def test_role_lattice_order() -> None:
    assert tenancy.role_rank(Role.OWNER) > tenancy.role_rank(Role.ADMIN)
    assert tenancy.role_rank(Role.ADMIN) > tenancy.role_rank(Role.EDITOR)
    assert tenancy.role_rank(Role.EDITOR) > tenancy.role_rank(Role.VIEWER)
    assert tenancy.role_at_least(Role.ADMIN, Role.EDITOR)
    assert not tenancy.role_at_least(Role.VIEWER, Role.EDITOR)
    assert not tenancy.role_at_least(None, Role.VIEWER)


def test_effective_role_picks_strongest() -> None:
    assert tenancy.effective_role(Role.VIEWER, Role.ADMIN) is Role.ADMIN
    assert tenancy.effective_role(None, Role.EDITOR) is Role.EDITOR
    assert tenancy.effective_role(None, None) is None


def test_permission_matrix_is_a_superset_lattice() -> None:
    viewer = tenancy.role_permissions(Role.VIEWER)
    editor = tenancy.role_permissions(Role.EDITOR)
    admin = tenancy.role_permissions(Role.ADMIN)
    owner = tenancy.role_permissions(Role.OWNER)
    # Each higher role is a strict superset of the next lower one.
    assert viewer < editor < admin <= owner
    assert owner == frozenset(Permission)  # owner has the whole catalogue


@pytest.mark.parametrize(
    ("role", "permission", "granted"),
    [
        (Role.VIEWER, Permission.BOOK_READ, True),
        (Role.VIEWER, Permission.BOOK_WRITE, False),
        (Role.VIEWER, Permission.MEMBER_INVITE, False),
        (Role.EDITOR, Permission.BOOK_WRITE, True),
        (Role.EDITOR, Permission.RENDER_ENQUEUE, True),
        (Role.EDITOR, Permission.MEMBER_INVITE, False),
        (Role.ADMIN, Permission.MEMBER_INVITE, True),
        (Role.ADMIN, Permission.QUOTA_MANAGE, True),
        (Role.ADMIN, Permission.ORG_DELETE, False),  # owner-only
        (Role.OWNER, Permission.ORG_DELETE, True),
        (Role.OWNER, Permission.BILLING_MANAGE, True),
    ],
)
def test_role_grants_matrix(role: Role, permission: Permission, granted: bool) -> None:
    assert tenancy.role_grants(role, permission) is granted


def test_require_grants_and_denies() -> None:
    ctx = _ctx(role=Role.EDITOR)
    # Granted permission returns the context.
    assert tenancy.require(Permission.BOOK_WRITE, ctx) is ctx
    assert tenancy.check(Permission.BOOK_READ, ctx)
    # Missing permission raises a typed denial.
    with pytest.raises(PermissionDenied) as exc:
        tenancy.require(Permission.MEMBER_INVITE, ctx)
    assert exc.value.permission is Permission.MEMBER_INVITE
    assert exc.value.reason == "insufficient_role"


def test_require_blocks_cross_tenant_even_with_permission() -> None:
    ctx = _ctx("abc", role=Role.OWNER)
    # Owner of org:abc cannot exercise a permission *against* org:other.
    assert not tenancy.check(Permission.BOOK_WRITE, ctx, within="org:other")
    with pytest.raises(PermissionDenied) as exc:
        tenancy.require(Permission.BOOK_WRITE, ctx, within="org:other")
    assert exc.value.reason == "tenant_mismatch"
    # ...but within its own tenant it passes.
    assert tenancy.require(Permission.BOOK_WRITE, ctx, within="org:abc") is ctx


def test_require_without_context_raises() -> None:
    with pytest.raises(NoTenantContext):
        tenancy.require(Permission.BOOK_READ)


def test_extra_capabilities_layer_on_top_of_role() -> None:
    ctx = TenantContext(
        tenant=TenantRef.organization("abc"),
        role=Role.VIEWER,
        extra_capabilities=frozenset({"member:*"}),
    )
    # The viewer role does not grant member:invite, but the API-key scope does.
    assert ctx.grants(Permission.MEMBER_INVITE)


# --------------------------------------------------------------------------- #
# Tenant-scoped query guard
# --------------------------------------------------------------------------- #


def test_tenant_scoped_appends_filter_and_verifies() -> None:
    ctx = _ctx("abc")
    stmt = guard_select(select(_Doc), _Doc, ctx)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "tenancy_test_docs.tenant_key" in compiled
    assert "org:abc" in compiled


def test_unscoped_query_raises() -> None:
    # A hand-written query that forgot the tenant filter must be rejected.
    with pytest.raises(UnscopedQueryError):
        assert_scoped(select(_Doc), _Doc)


def test_scoped_query_passes_verification() -> None:
    ctx = _ctx("abc")
    scoped = tenant_scoped(select(_Doc).where(_Doc.title == "x"), _Doc, ctx)
    # Both the business predicate and the tenant predicate are present.
    assert_scoped(scoped, _Doc)  # does not raise


def test_guard_blocks_cross_tenant_reads() -> None:
    # The filter value is the *active* tenant, so a query built for tenant A can
    # never read tenant B's rows — the literal in the WHERE is A's key.
    ctx_a = _ctx("aaa")
    stmt_a = guard_select(select(_Doc), _Doc, ctx_a)
    sql_a = str(stmt_a.compile(compile_kwargs={"literal_binds": True}))
    assert "org:aaa" in sql_a
    assert "org:bbb" not in sql_a


def test_guard_uses_active_context_when_none_passed() -> None:
    with use_tenant(_ctx("ctxonly")):
        stmt = guard_select(select(_Doc), _Doc)
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "org:ctxonly" in sql


def test_guard_raises_without_any_tenant() -> None:
    with pytest.raises(NoTenantContext):
        tenant_scoped(select(_Doc), _Doc)


def test_guard_rejects_untenanted_model() -> None:
    with pytest.raises(MissingTenantColumnError):
        tenant_scoped(select(_Untenanted), _Untenanted, _ctx("abc"))


def test_guard_in_filter_is_not_an_isolation() -> None:
    # An IN over many tenant keys is NOT a tenant scope; the verifier rejects it.
    stmt = select(_Doc).where(_Doc.tenant_key.in_(["org:a", "org:b"]))
    with pytest.raises(UnscopedQueryError):
        assert_scoped(stmt, _Doc)


def test_guard_or_buried_filter_is_not_credited() -> None:
    # A tenant equality buried inside an OR does not isolate.
    stmt = select(_Doc).where(
        (_Doc.tenant_key == "org:a") | (_Doc.title == "x")
    )
    with pytest.raises(UnscopedQueryError):
        assert_scoped(stmt, _Doc)


# --------------------------------------------------------------------------- #
# Per-tenant quota / spend envelopes
# --------------------------------------------------------------------------- #


def test_quota_within_envelope_allows() -> None:
    env = QuotaEnvelope(max_books=10, monthly_video_seconds=300.0)
    usage = Usage(books=3, video_seconds=100.0)
    d = tenancy.evaluate(QuotaResource.BOOKS, 5, envelope=env, usage=usage)
    assert d.allowed
    assert d.tenant_remaining == 7.0


def test_quota_breach_raises_tenant_scope() -> None:
    env = QuotaEnvelope(max_books=5)
    usage = Usage(books=4)
    with pytest.raises(QuotaExceeded) as exc:
        tenancy.reserve(QuotaResource.BOOKS, 2, envelope=env, usage=usage)
    assert exc.value.scope is QuotaScope.TENANT
    assert exc.value.resource is QuotaResource.BOOKS


def test_quota_zero_cap_is_unlimited_by_envelope() -> None:
    env = QuotaEnvelope()  # all zero == unlimited-by-envelope
    usage = Usage(books=1_000_000)
    d = tenancy.evaluate(QuotaResource.BOOKS, 1, envelope=env, usage=usage)
    assert d.allowed
    assert d.tenant_remaining == float("inf")


def test_quota_composes_with_global_ceiling_binding_global() -> None:
    # Tenant envelope leaves 200s but the global ceiling only has 50s left.
    env = QuotaEnvelope(monthly_video_seconds=300.0)
    usage = Usage(video_seconds=100.0)
    gs = GlobalState(video_seconds_remaining=50.0)
    d = tenancy.evaluate(
        QuotaResource.VIDEO_SECONDS, 100, envelope=env, usage=usage, global_state=gs
    )
    assert not d.allowed
    assert d.binding_scope is QuotaScope.GLOBAL
    assert d.remaining == 50.0


def test_quota_composes_with_global_ceiling_binding_tenant() -> None:
    # Global has plenty, the tenant envelope is the tighter bound.
    env = QuotaEnvelope(monthly_video_seconds=120.0)
    usage = Usage(video_seconds=100.0)
    gs = GlobalState(video_seconds_remaining=10_000.0)
    with pytest.raises(QuotaExceeded) as exc:
        tenancy.reserve(
            QuotaResource.VIDEO_SECONDS, 100, envelope=env, usage=usage, global_state=gs
        )
    assert exc.value.scope is QuotaScope.TENANT


def test_usage_accumulates() -> None:
    u = Usage().with_charge(QuotaResource.VIDEO_SECONDS, 30.0)
    u = u.with_charge(QuotaResource.VIDEO_SECONDS, 20.0)
    u = u.with_charge(QuotaResource.BOOKS, 1)
    assert u.video_seconds == 50.0
    assert u.books == 1


# --------------------------------------------------------------------------- #
# Asset-prefix isolation
# --------------------------------------------------------------------------- #


def test_asset_scoped_key_and_isolation() -> None:
    ctx_a = _ctx("aaa")
    key = tenancy.scoped_key("clips/book1/shot1.mp4", ctx_a)
    assert key == "t/org_aaa/clips/book1/shot1.mp4"
    assert tenancy.belongs_to_tenant(key, ctx_a)
    # A different tenant's context does not recognise tenant A's key.
    ctx_b = _ctx("bbb")
    assert not tenancy.belongs_to_tenant(key, ctx_b)
    with pytest.raises(tenancy.CrossTenantAssetError):
        tenancy.assert_in_tenant(key, ctx_b)


def test_asset_scoped_key_is_idempotent() -> None:
    ctx = _ctx("aaa")
    once = tenancy.scoped_key("clips/b/s.mp4", ctx)
    twice = tenancy.scoped_key(once, ctx)
    assert once == twice


def test_asset_listing_prefix_confined_to_tenant() -> None:
    ctx = _ctx("aaa")
    assert tenancy.listing_prefix("clips", ctx) == "t/org_aaa/clips"
    assert tenancy.listing_prefix("", ctx) == "t/org_aaa/"


def test_org_and_workspace_prefixes_are_disjoint() -> None:
    org = tenancy.derive_prefix(TenantRef.organization("x").key)
    ws = tenancy.derive_prefix(TenantRef.workspace("x", org_id="x").key)
    assert org != ws  # org:x vs ws:x map to different prefixes


# --------------------------------------------------------------------------- #
# Context propagation (contextvars)
# --------------------------------------------------------------------------- #


def test_context_default_is_none_and_require_raises() -> None:
    assert tenancy.current_tenant() is None
    with pytest.raises(NoTenantContext):
        tenancy.require_tenant()


def test_use_tenant_scopes_and_restores() -> None:
    assert tenancy.current_tenant() is None
    with use_tenant(_ctx("abc")) as ctx:
        assert tenancy.current_tenant() is ctx
        assert tenancy.current_tenant_key() == "org:abc"
    assert tenancy.current_tenant() is None  # restored on exit


def test_use_tenant_restores_on_exception() -> None:
    with pytest.raises(ValueError, match="boom"), use_tenant(_ctx("abc")):
        raise ValueError("boom")
    assert tenancy.current_tenant() is None


def test_context_propagates_into_child_task() -> None:
    async def scenario() -> str | None:
        with use_tenant(_ctx("parent")):
            # A child task created inside the scope inherits a copy of the context.
            return await asyncio.create_task(_read_key())

    async def _read_key() -> str | None:
        return tenancy.current_tenant_key()

    assert asyncio.run(scenario()) == "org:parent"


def test_child_rebind_does_not_leak_to_parent() -> None:
    async def scenario() -> tuple[str | None, str | None]:
        with use_tenant(_ctx("parent")):
            await asyncio.create_task(_rebind())
            # The child's rebind must not leak back into the parent context.
            return tenancy.current_tenant_key(), None

    async def _rebind() -> None:
        with use_tenant(_ctx("child")):
            assert tenancy.current_tenant_key() == "org:child"

    parent_after, _ = asyncio.run(scenario())
    assert parent_after == "org:parent"


def test_contextvars_isolated_across_copy_context() -> None:
    # Two independent contexts see independent tenant values.
    def in_ctx(key: str) -> str | None:
        with use_tenant(_ctx(key)):
            return tenancy.current_tenant_key()

    c1 = contextvars.copy_context()
    c2 = contextvars.copy_context()
    assert c1.run(in_ctx, "one") == "org:one"
    assert c2.run(in_ctx, "two") == "org:two"
    assert tenancy.current_tenant() is None  # neither leaked into the caller


# --------------------------------------------------------------------------- #
# TenancyService end-to-end (in-memory fakes)
# --------------------------------------------------------------------------- #


def _service(
    store: InMemoryTenancyStore, *, global_remaining: float = float("inf")
) -> TenancyService:
    return TenancyService(
        orgs=store.orgs,
        workspaces=store.workspaces,
        memberships=store.memberships,
        quotas=store.quotas,
        global_remaining=lambda: global_remaining,
    )


def test_service_resolves_context_for_member() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(owner_user_id="owner", org_id="org1")
    store.add_member(user_id="ed", org=org, role=Role.EDITOR)
    svc = _service(store)

    ctx = svc.resolve_context(user_id="ed", org_id="org1")
    assert ctx is not None
    assert ctx.role is Role.EDITOR
    assert ctx.tenant_key == "org:org1"
    assert ctx.asset_prefix == "t/org_org1"


def test_service_denies_non_member() -> None:
    store = InMemoryTenancyStore()
    store.seed_org(owner_user_id="owner", org_id="org1")
    svc = _service(store)
    assert svc.resolve_context(user_id="stranger", org_id="org1") is None


def test_service_workspace_role_overrides_org_role() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(owner_user_id="owner", org_id="org1")
    ws = store.seed_workspace(org, workspace_id="ws1")
    # Org viewer, but workspace admin -> effective role is admin in the workspace.
    store.add_member(user_id="u", org=org, role=Role.VIEWER)
    store.add_member(user_id="u", org=org, role=Role.ADMIN, workspace=ws)
    svc = _service(store)

    ctx = svc.resolve_context(user_id="u", org_id="org1", workspace_id="ws1")
    assert ctx is not None
    assert ctx.role is Role.ADMIN
    assert ctx.tenant_key == "ws:ws1"


def test_service_workspace_envelope_tightens_org_envelope() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(
        owner_user_id="o", org_id="org1", envelope=QuotaEnvelope(max_books=100)
    )
    store.seed_workspace(
        org, workspace_id="ws1", envelope=QuotaEnvelope(max_books=5)
    )
    store.add_member(user_id="o", org=org, role=Role.OWNER)
    svc = _service(store)
    ctx = svc.resolve_context(user_id="o", org_id="org1", workspace_id="ws1")
    assert ctx is not None
    env = svc.effective_envelope(ctx)
    assert env.max_books == 5  # the tighter workspace cap wins


def test_service_enforces_quota_then_records() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(
        owner_user_id="o", org_id="org1", envelope=QuotaEnvelope(max_books=2)
    )
    store.add_member(user_id="o", org=org, role=Role.OWNER)
    svc = _service(store)
    ctx = svc.resolve_context(user_id="o", org_id="org1")
    assert ctx is not None

    svc.check_quota(ctx, QuotaResource.BOOKS, 1)
    svc.record_usage(ctx, QuotaResource.BOOKS, 1)
    svc.check_quota(ctx, QuotaResource.BOOKS, 1)
    svc.record_usage(ctx, QuotaResource.BOOKS, 1)
    # Third book breaches the cap of 2.
    with pytest.raises(QuotaExceeded):
        svc.check_quota(ctx, QuotaResource.BOOKS, 1)


def test_service_quota_composes_with_injected_global_ceiling() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(
        owner_user_id="o",
        org_id="org1",
        envelope=QuotaEnvelope(monthly_video_seconds=1000.0),
    )
    store.add_member(user_id="o", org=org, role=Role.OWNER)
    # Global ceiling head-room is only 30s — the binding cap despite a 1000s tenant.
    svc = _service(store, global_remaining=30.0)
    ctx = svc.resolve_context(user_id="o", org_id="org1")
    assert ctx is not None
    d = svc.check_quota(ctx, QuotaResource.VIDEO_SECONDS, 20)
    assert d.allowed
    with pytest.raises(QuotaExceeded) as exc:
        svc.check_quota(ctx, QuotaResource.VIDEO_SECONDS, 40)
    assert exc.value.scope is QuotaScope.GLOBAL


def test_service_require_uses_resolved_role() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(owner_user_id="o", org_id="org1")
    store.add_member(user_id="v", org=org, role=Role.VIEWER)
    svc = _service(store)
    ctx = svc.resolve_context(user_id="v", org_id="org1")
    assert ctx is not None
    assert svc.require(Permission.BOOK_READ, ctx) is ctx
    with pytest.raises(PermissionDenied):
        svc.require(Permission.BOOK_WRITE, ctx)


def test_service_seats_count_active_members() -> None:
    store = InMemoryTenancyStore()
    org = store.seed_org(owner_user_id="o", org_id="org1")  # owner is one member
    store.add_member(user_id="a", org=org, role=Role.EDITOR)
    store.add_member(user_id="b", org=org, role=Role.VIEWER)
    assert store.memberships.count_active_in_org("org1") == 3
