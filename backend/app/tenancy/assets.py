"""Per-tenant object-store prefix isolation + config overrides.

A tenant's media must live under its *own* key prefix so a presigned URL, a
listing, or a stray key collision can never cross a tenant boundary. This module
owns the mapping from a tenant key to its object-store prefix and the helpers
that:

* **scope a key** onto a tenant prefix (:func:`scoped_key`) — so the existing
  :class:`app.storage.object_store.Keys` layout (``clips/<book>/<shot>.mp4`` …)
  becomes ``t/<tenant>/clips/<book>/<shot>.mp4``;
* **verify a key** belongs to the active tenant (:func:`assert_in_tenant` /
  :func:`key_tenant`) — the row-level isolation companion for any code that
  reads a stored key back (e.g. before issuing a presigned URL);
* **scope a listing prefix** (:func:`listing_prefix`) so a ``list_objects`` can
  only ever see one tenant's objects.

Prefixes are deterministic and filesystem/S3-safe (the tenant key's
``org:``/``ws:`` separator is normalised to ``_`` so it is a single path
segment). The prefix is *stable* for a tenant for the life of the platform, so
it is computed purely from the tenant key — no DB round-trip.

The :class:`TenantConfig` half layers per-tenant config overrides over the
global :class:`~app.core.config.Settings` at *read* time (never mutating the
shared Settings singleton): a tenant can, e.g., pin a render-mode ceiling or a
distinct bucket without a code change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.tenancy.context import TenantContext, require_tenant

#: The top-level namespace segment all tenant-scoped assets live under. Keeps
#: tenant media visibly partitioned from any legacy/global keys.
TENANT_NAMESPACE = "t"


class CrossTenantAssetError(RuntimeError):
    """Raised when an object key does not belong to the active tenant's prefix."""


def _sanitise(tenant_key: str) -> str:
    """Normalise a tenant key (``org:abc``) into one safe path segment.

    ``:`` and ``/`` are not valid inside a single key segment, so collapse them
    to ``_``. The result is injective over well-formed tenant keys (``org:`` vs
    ``ws:`` prefixes keep org and workspace namespaces disjoint).
    """
    return tenant_key.replace(":", "_").replace("/", "_")


def tenant_prefix(ctx: TenantContext | None = None) -> str:
    """The object-store key prefix for a tenant (no trailing slash).

    Uses the context's explicit :attr:`TenantContext.asset_prefix` when set
    (so an override can be persisted), else derives it deterministically from
    the tenant key.
    """
    resolved = ctx if ctx is not None else require_tenant()
    if resolved.asset_prefix:
        return resolved.asset_prefix.rstrip("/")
    return f"{TENANT_NAMESPACE}/{_sanitise(resolved.tenant_key)}"


def derive_prefix(tenant_key: str) -> str:
    """The canonical prefix for a tenant key, independent of any context."""
    return f"{TENANT_NAMESPACE}/{_sanitise(tenant_key)}"


def scoped_key(key: str, ctx: TenantContext | None = None) -> str:
    """Prepend the active tenant's prefix to a global object ``key``.

    ``key`` is a path from the existing :class:`Keys` layout (e.g.
    ``clips/<book>/<shot>.mp4``). The result is idempotent: a key already under
    the tenant prefix is returned unchanged, so double-scoping is safe.
    """
    prefix = tenant_prefix(ctx)
    cleaned = key.lstrip("/")
    if cleaned == prefix or cleaned.startswith(f"{prefix}/"):
        return cleaned
    return f"{prefix}/{cleaned}"


def listing_prefix(subpath: str = "", ctx: TenantContext | None = None) -> str:
    """A ``list_objects`` prefix confined to the tenant (optionally a subpath).

    Listing with this prefix can only ever enumerate the tenant's own objects.
    """
    prefix = tenant_prefix(ctx)
    if subpath:
        return f"{prefix}/{subpath.strip('/')}"
    return f"{prefix}/"


def key_tenant_prefix(key: str) -> str | None:
    """Extract the ``t/<tenant>`` prefix from a scoped key, or ``None``.

    Used to attribute a stored key back to a tenant for the visibility check.
    """
    cleaned = key.lstrip("/")
    parts = cleaned.split("/", 2)
    if len(parts) >= 2 and parts[0] == TENANT_NAMESPACE:
        return f"{parts[0]}/{parts[1]}"
    return None


def belongs_to_tenant(key: str, ctx: TenantContext | None = None) -> bool:
    """Whether ``key`` lives under the active tenant's prefix."""
    prefix = tenant_prefix(ctx)
    cleaned = key.lstrip("/")
    return cleaned == prefix or cleaned.startswith(f"{prefix}/")


def assert_in_tenant(key: str, ctx: TenantContext | None = None) -> None:
    """Raise :class:`CrossTenantAssetError` unless ``key`` is the tenant's.

    The fail-closed gate to call before returning a presigned URL or streaming
    bytes for a key read out of a row, so a tampered/foreign key is rejected.
    """
    if not belongs_to_tenant(key, ctx):
        raise CrossTenantAssetError(
            f"object key {key!r} is outside tenant prefix {tenant_prefix(ctx)!r}"
        )


# --------------------------------------------------------------------------- #
# Per-tenant config overrides
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TenantConfig:
    """Per-tenant overrides layered over the global ``Settings`` at read time.

    A small, explicit allow-list bag (``bucket``, render-mode ceiling, …) merged
    over the global settings without ever mutating the shared singleton. Reads
    fall through to ``defaults`` when the tenant has no override for a key.
    """

    overrides: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """The tenant's override for ``key`` else ``default``."""
        return self.overrides.get(key, default)

    def resolve(self, key: str, defaults: Mapping[str, Any]) -> Any:
        """The tenant override for ``key`` else the global ``defaults`` value."""
        if key in self.overrides:
            return self.overrides[key]
        return defaults.get(key)

    def merged(self, defaults: Mapping[str, Any]) -> dict[str, Any]:
        """A new dict of ``defaults`` with the tenant overrides applied on top."""
        out = dict(defaults)
        out.update(self.overrides)
        return out

    @classmethod
    def from_context(cls, ctx: TenantContext | None = None) -> TenantConfig:
        """Build from a context's :attr:`TenantContext.config_overrides`."""
        resolved = ctx if ctx is not None else require_tenant()
        return cls(overrides=dict(resolved.config_overrides))


__all__ = [
    "TENANT_NAMESPACE",
    "CrossTenantAssetError",
    "TenantConfig",
    "assert_in_tenant",
    "belongs_to_tenant",
    "derive_prefix",
    "key_tenant_prefix",
    "listing_prefix",
    "scoped_key",
    "tenant_prefix",
]
