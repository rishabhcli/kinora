"""End-to-end tests of the composed plane (factory + SDK + cache + audit)."""

from __future__ import annotations

import pytest

from app.platform.authz import (
    AccessDeniedError,
    Effect,
    InMemoryDecisionCache,
    InMemoryDecisionLog,
    Resource,
    Subject,
    build_plane,
    build_relation_graph,
)
from app.platform.authz.rebac import RelationTuple

# -- the default plane: native engines only ---------------------------------- #


def test_personal_owner_allowed_via_abac() -> None:
    plane = build_plane(include_auth_rbac=False)
    d = plane.check_sync("alice", "book:edit", Resource.of("book", "1", owner="alice"))
    assert d.effect is Effect.ALLOW
    assert any(r.rule == "personal-owner" for r in d.reasons)


def test_stranger_denied_by_default() -> None:
    plane = build_plane(include_auth_rbac=False)
    d = plane.check_sync("bob", "book:edit", Resource.of("book", "1", owner="alice"))
    assert d.effect is Effect.DENY


def test_admin_override_allows_anything() -> None:
    plane = build_plane(include_auth_rbac=False)
    d = plane.check_sync(
        Subject.user("root", is_admin=True),
        "book:delete",
        Resource.of("book", "1", owner="someone-else"),
    )
    assert d.effect is Effect.ALLOW
    assert any(r.rule == "admin-override" for r in d.reasons)


def test_tenant_isolation_denies_cross_tenant() -> None:
    plane = build_plane(include_auth_rbac=False)
    d = plane.check_sync(
        Subject.user("alice", tenant="t1", roles=["editor"]),
        "book:read",
        Resource.of("book", "1", tenant="t2"),
    )
    assert d.effect is Effect.DENY
    assert any(r.rule == "tenant-isolation" for r in d.reasons)


def test_rbac_role_grants_action() -> None:
    # the auth role catalogue: editor holds book:read et al.
    plane = build_plane()
    d = plane.check_sync(
        Subject.user("alice", roles=["editor"]),
        "books:read",  # uses the legacy permission vocabulary
        Resource.of("book", "1"),
    )
    assert d.effect is Effect.ALLOW


# -- relationship grants via tuples ------------------------------------------ #


def test_workspace_member_reads_attached_book_via_tuples() -> None:
    store_graph = build_relation_graph()
    store = store_graph._store  # type: ignore[attr-defined]
    store.write(RelationTuple.of("workspace:w", "viewer", "user:bob"))
    store.write(RelationTuple.of("book:b", "parent", "workspace:w"))
    plane = build_plane(tuple_store=store, include_auth_rbac=False)
    d = plane.check_sync("bob", "book:read", Resource.of("book", "b"))
    assert d.effect is Effect.ALLOW
    assert any(r.source == "rebac" for r in d.reasons)


def test_list_objects_reverse_index_through_plane() -> None:
    from app.platform.authz.rebac import InMemoryTupleStore

    store = InMemoryTupleStore()
    store.write(RelationTuple.of("book:1", "owner", "user:alice"))
    store.write(RelationTuple.of("book:2", "owner", "user:bob"))
    store.write(RelationTuple.of("workspace:w", "editor", "user:alice"))
    store.write(RelationTuple.of("book:3", "parent", "workspace:w"))
    plane = build_plane(tuple_store=store)
    readable = plane.list_objects("book", "book:read", "alice")
    assert readable == frozenset({"1", "3"})


def test_list_objects_empty_without_rebac_engine() -> None:
    # a plane with no rebac engine returns empty
    from app.platform.authz.abac import AbacEngine
    from app.platform.authz.sdk import AuthorizationPlane

    plane = AuthorizationPlane([AbacEngine([])])
    assert plane.list_objects("book", "book:read", "alice") == frozenset()


# -- cache + audit integration ----------------------------------------------- #


def test_cache_hit_on_second_check_sync() -> None:
    cache = InMemoryDecisionCache(ttl_s=100)
    plane = build_plane(cache=cache, include_auth_rbac=False)
    res = Resource.of("book", "1", owner="alice")
    first = plane.check_sync("alice", "book:edit", res)
    assert not first.cached
    second = plane.check_sync("alice", "book:edit", res)
    assert second.cached
    assert cache.hits == 1


def test_decision_log_records_every_check() -> None:
    log = InMemoryDecisionLog()
    plane = build_plane(decision_log=log, include_auth_rbac=False)
    plane.check_sync("alice", "book:edit", Resource.of("book", "1", owner="alice"))
    plane.check_sync("bob", "book:edit", Resource.of("book", "1", owner="alice"))
    assert len(log) == 2
    assert len(log.denials()) == 1


def test_invalidate_subject_drops_cache() -> None:
    cache = InMemoryDecisionCache(ttl_s=100)
    plane = build_plane(cache=cache, include_auth_rbac=False)
    res = Resource.of("book", "1", owner="alice")
    plane.check_sync("alice", "book:edit", res)
    assert plane.invalidate_subject("user:alice") == 1
    again = plane.check_sync("alice", "book:edit", res)
    assert not again.cached  # re-evaluated after invalidation


# -- async check + require --------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_check_and_require() -> None:
    plane = build_plane(include_auth_rbac=False)
    res = Resource.of("book", "1", owner="alice")
    assert await plane.is_allowed("alice", "book:edit", res)
    decision = await plane.require("alice", "book:edit", res)
    assert decision.allowed


@pytest.mark.asyncio
async def test_require_raises_on_deny() -> None:
    plane = build_plane(include_auth_rbac=False)
    with pytest.raises(AccessDeniedError) as exc:
        await plane.require("bob", "book:edit", Resource.of("book", "1", owner="alice"))
    assert exc.value.decision.effect is Effect.DENY


# -- full plane with all adapters wired -------------------------------------- #


@pytest.mark.asyncio
async def test_full_plane_with_mcp_adapter_denies_forged_book() -> None:
    async def exists(book_id: str) -> bool:
        return book_id == "real"

    plane = build_plane(book_exists=exists, include_auth_rbac=False)
    # a forged book → MCP adapter denies even though owner would allow on a real one
    d = await plane.check("alice", "book:read", Resource.of("book", "forged", owner="alice"))
    assert d.effect is Effect.DENY
    assert any(r.rule == "mcp:book-existence" for r in d.reasons)
    # a real, owned book → allowed
    d2 = await plane.check("alice", "book:read", Resource.of("book", "real", owner="alice"))
    assert d2.effect is Effect.ALLOW


def test_with_engine_appends() -> None:
    from app.platform.authz.abac import AbacEngine

    plane = build_plane(include_auth_rbac=False)
    n = len(plane.engines)
    plane2 = plane.with_engine(AbacEngine([]))
    assert len(plane2.engines) == n + 1
    assert len(plane.engines) == n  # original unchanged (immutable)


def test_deny_overrides_beats_rbac_wildcard() -> None:
    # The RBAC `admin` role holds the `*` wildcard (ALLOW), but the subject is
    # tenant-scoped to t1 and the resource is in t2 *without* the explicit
    # `is_admin` ABAC attribute. Tenant-isolation DENY must override the RBAC
    # wildcard ALLOW under deny-overrides. (Crossing tenants requires the
    # explicit `is_admin` attribute — distinct from holding the admin *role* —
    # which the admin-override ABAC rule keys on; see the admin-override test.)
    plane = build_plane()
    d = plane.check_sync(
        Subject.user("alice", roles=["admin"], tenant="t1"),
        "books:read",
        Resource.of("book", "1", tenant="t2"),
    )
    assert d.effect is Effect.DENY


def test_explicit_admin_attribute_crosses_tenants() -> None:
    # The `is_admin` ABAC attribute (first-applicable, runs before tenant
    # isolation) lets a true admin cross tenant boundaries.
    plane = build_plane()
    d = plane.check_sync(
        Subject.user("root", is_admin=True, tenant="t1"),
        "books:read",
        Resource.of("book", "1", tenant="t2"),
    )
    assert d.effect is Effect.ALLOW


def test_non_admin_tenant_isolation_denies_over_role() -> None:
    plane = build_plane()
    d = plane.check_sync(
        Subject.user("alice", roles=["editor"], tenant="t1"),
        "books:read",
        Resource.of("book", "1", tenant="t2"),
    )
    assert d.effect is Effect.DENY  # tenant DENY overrides the editor role ALLOW
