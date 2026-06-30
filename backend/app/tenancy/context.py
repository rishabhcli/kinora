"""The per-request tenant context and its ``contextvars`` propagation.

Multi-tenant isolation hinges on one invariant: every scoped operation knows
*which tenant it is acting for*, and that knowledge travels with the logical task
without being threaded through every function signature. We carry it in a
:class:`contextvars.ContextVar`, which is the right tool because:

* it is **task-local** — an ``asyncio`` task (one request) sees its own value
  even when thousands run concurrently on one event loop;
* it **propagates** into the coroutines/tasks a request spawns (a ``Context`` is
  copied when a task is created), so a downstream repository call sees the same
  tenant without an explicit argument; and
* it is **isolated** — a child task that rebinds the var does not leak the change
  back to its parent (verified by a test).

The public surface is small and fail-closed:

* :func:`current_tenant` — the active context or ``None``.
* :func:`require_tenant` — the active context or raise :class:`NoTenantContext`.
* :func:`bind_tenant` / :func:`reset_tenant` — imperative set/clear returning a
  token (the FastAPI-middleware style).
* :func:`use_tenant` — a context-manager / sync-friendly scope that always
  restores the previous value, even on exception.

The :class:`TenantContext` itself is an immutable value object: a tenant key
(``org:…`` / ``ws:…``), the principal, the effective role, and the resolved
asset prefix + config overrides. It deliberately holds *no* live DB/session
handle so it is cheap to copy across task boundaries.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.tenancy.roles import Permission, Role, role_grants

# --------------------------------------------------------------------------- #
# Tenant identity
# --------------------------------------------------------------------------- #


class TenantKind(StrEnum):
    """Whether a tenant is an organization or one of its workspaces.

    A workspace is *nested* under an org; isolation is enforced at the tenant
    key (the most-specific scope the principal is acting within), while the org
    id is kept so org-level quota/spend envelopes can be composed.
    """

    ORG = "org"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class TenantRef:
    """A stable, comparable reference to the tenant a context is bound to.

    ``key`` is the canonical isolation token used everywhere downstream (query
    guard, asset prefix, quota scope): ``"org:<id>"`` or ``"ws:<id>"``.
    """

    kind: TenantKind
    tenant_id: str
    #: The owning org id. Equals ``tenant_id`` when ``kind is ORG``.
    org_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.org_id:
            raise ValueError("org_id must be non-empty")

    @property
    def key(self) -> str:
        """The canonical isolation token (``org:<id>`` / ``ws:<id>``)."""
        prefix = "org" if self.kind is TenantKind.ORG else "ws"
        return f"{prefix}:{self.tenant_id}"

    @classmethod
    def organization(cls, org_id: str) -> TenantRef:
        """An org-scoped tenant reference."""
        return cls(kind=TenantKind.ORG, tenant_id=org_id, org_id=org_id)

    @classmethod
    def workspace(cls, workspace_id: str, *, org_id: str) -> TenantRef:
        """A workspace-scoped tenant reference under ``org_id``."""
        return cls(kind=TenantKind.WORKSPACE, tenant_id=workspace_id, org_id=org_id)


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The immutable, task-local context every scoped operation acts within.

    Produced once per request (by the route dependency / middleware) from the
    authenticated principal + the tenant they selected, then propagated via the
    :data:`_TENANT` context var. Holds no live handles so it is safe to copy
    across task boundaries.
    """

    tenant: TenantRef
    #: The acting user (account id). ``None`` only for internal/system contexts.
    principal_id: str | None = None
    #: The principal's effective role within this tenant (collapsed org+ws role).
    role: Role | None = None
    #: Extra string capabilities (e.g. from an API key's scopes) layered on top.
    extra_capabilities: frozenset[str] = frozenset()
    #: Object-store key prefix isolating this tenant's assets (no trailing slash).
    asset_prefix: str = ""
    #: Per-tenant config overrides (merged over global Settings at read time).
    config_overrides: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tenant_key(self) -> str:
        """Shortcut for :attr:`TenantRef.key`."""
        return self.tenant.key

    @property
    def org_id(self) -> str:
        """The owning org id (for org-level envelope composition)."""
        return self.tenant.org_id

    def grants(self, permission: Permission) -> bool:
        """Whether this context's role/capabilities grant ``permission``.

        Pure: the role lattice plus any extra string capabilities. The stateful
        ``require``/``check`` decorate this with tenant-mismatch fail-close.
        """
        if role_grants(self.role, permission):
            return True
        from app.tenancy.roles import has_capability

        return has_capability(self.extra_capabilities, str(permission))


# --------------------------------------------------------------------------- #
# The context variable + accessors
# --------------------------------------------------------------------------- #


class NoTenantContext(RuntimeError):  # noqa: N818 - public contract name
    """Raised when a tenant-scoped operation runs with no resolved tenant.

    This is the fail-closed default: code that *should* be tenant-scoped never
    silently runs unscoped — it raises. (See also
    :class:`app.tenancy.guard.UnscopedQueryError` for the query-level guard.)
    """


# The single source of truth for "who am I acting for right now". Default
# ``None`` means *no tenant resolved* — fail closed, never a wildcard.
_TENANT: ContextVar[TenantContext | None] = ContextVar("kinora_tenant_context", default=None)


def current_tenant() -> TenantContext | None:
    """The active :class:`TenantContext`, or ``None`` if none is bound."""
    return _TENANT.get()


def current_tenant_key() -> str | None:
    """The active tenant's isolation key, or ``None`` if none is bound."""
    ctx = _TENANT.get()
    return ctx.tenant_key if ctx is not None else None


def require_tenant() -> TenantContext:
    """The active :class:`TenantContext`, or raise :class:`NoTenantContext`."""
    ctx = _TENANT.get()
    if ctx is None:
        raise NoTenantContext("no tenant context is bound to the current task")
    return ctx


def bind_tenant(ctx: TenantContext) -> Token[TenantContext | None]:
    """Imperatively bind ``ctx`` to the current task, returning a reset token.

    Mirror of the FastAPI-middleware idiom: capture the returned token and pass
    it to :func:`reset_tenant` in a ``finally`` to restore the previous value.
    Prefer :func:`use_tenant` where a ``with`` block fits.
    """
    return _TENANT.set(ctx)


def reset_tenant(token: Token[TenantContext | None]) -> None:
    """Restore the tenant var to the value captured before :func:`bind_tenant`."""
    _TENANT.reset(token)


@contextlib.contextmanager
def use_tenant(ctx: TenantContext | None) -> Iterator[TenantContext | None]:
    """Scope ``ctx`` (or *no tenant* when ``None``) for the duration of the block.

    Always restores the previous value on exit, including on exception — so a
    handler that raises never leaks one request's tenant into the next.
    """
    token = _TENANT.set(ctx)
    try:
        yield ctx
    finally:
        _TENANT.reset(token)


__all__ = [
    "NoTenantContext",
    "TenantContext",
    "TenantKind",
    "TenantRef",
    "bind_tenant",
    "current_tenant",
    "current_tenant_key",
    "require_tenant",
    "reset_tenant",
    "use_tenant",
]
