"""One-call construction of a fully-wired Kinora authorization plane.

Most callers do not want to assemble engines by hand; they want a plane that
already encodes Kinora's model. :func:`build_plane` wires:

* an **RBAC engine** over the legacy auth role catalogue (presets);
* an **ABAC engine** with the default invariant rules (admin override, personal
  owner, tenant isolation);
* a **policy DSL engine** over any extra policy modules passed in;
* a **ReBAC engine** over the Kinora namespaces + action→relation map, backed by
  a tuple store (an in-memory one by default; a DB-backed store can be injected);
* a **decision cache** and a **decision log**;
* the four **adapters** (auth RBAC / workspaces / MCP / moderation) when their
  backing dependency is supplied — so a deployment that has a workspace service
  factory gets the workspaces grant folded in, and one that doesn't simply omits
  that engine. The plane is identical-shaped either way.

The engine *order* matters for ``FIRST_APPLICABLE`` and for the reason trail, but
under the default ``DENY_OVERRIDES`` the order only affects explanation ordering,
not the verdict — so the factory uses a deterministic, legible order: native
engines first (RBAC, ABAC, ReBAC, policy), then adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from app.platform.authz.abac import AbacEngine, AbacRule
from app.platform.authz.adapters import (
    AuthRbacAdapter,
    McpBookScopeAdapter,
    ModerationPolicyAdapter,
    WorkspaceAuthzAdapter,
)
from app.platform.authz.audit import DecisionLog, InMemoryDecisionLog
from app.platform.authz.cache import DecisionCache, InMemoryDecisionCache
from app.platform.authz.combining import CombiningAlgorithm
from app.platform.authz.dsl import PolicyEngine
from app.platform.authz.engine import AuthorizationEngine
from app.platform.authz.presets import (
    ACTION_RELATION,
    DEFAULT_ABAC_RULES,
    KINORA_NAMESPACES,
    auth_role_catalogue,
)
from app.platform.authz.rebac import (
    InMemoryTupleStore,
    RebacEngine,
    RelationGraph,
    TupleStore,
)
from app.platform.authz.sdk import AuthorizationPlane


def build_plane(
    *,
    tuple_store: TupleStore | None = None,
    abac_rules: Iterable[AbacRule] = DEFAULT_ABAC_RULES,
    policy_sources: Sequence[str] = (),
    cache: DecisionCache | None = None,
    decision_log: DecisionLog | None = None,
    algorithm: CombiningAlgorithm = CombiningAlgorithm.DENY_OVERRIDES,
    include_auth_rbac: bool = True,
    workspace_service_factory: Callable[[], Any] | None = None,
    book_exists: Callable[[str], Awaitable[bool]] | None = None,
    include_moderation: bool = False,
) -> AuthorizationPlane:
    """Assemble the standard Kinora plane (see module docstring for the wiring).

    All adapter dependencies are optional: pass ``workspace_service_factory`` to
    fold the DB-backed workspaces grant in, ``book_exists`` for the MCP
    book-scope adapter, ``include_moderation=True`` for the content gate. Omitted
    adapters are simply not added.
    """
    store = tuple_store if tuple_store is not None else InMemoryTupleStore()
    graph = RelationGraph(KINORA_NAMESPACES, store)

    engines: list[AuthorizationEngine] = [
        _rbac_engine(),
        AbacEngine(abac_rules),
        RebacEngine(graph, ACTION_RELATION),
    ]
    if policy_sources:
        engines.append(PolicyEngine.from_sources(*policy_sources))
    if include_auth_rbac:
        engines.append(AuthRbacAdapter())
    if workspace_service_factory is not None:
        engines.append(WorkspaceAuthzAdapter(workspace_service_factory))
    if book_exists is not None:
        engines.append(McpBookScopeAdapter(book_exists=book_exists))
    if include_moderation:
        engines.append(ModerationPolicyAdapter())

    return AuthorizationPlane(
        engines,
        algorithm=algorithm,
        cache=cache if cache is not None else InMemoryDecisionCache(),
        # NB: ``or`` would be wrong here — an *empty* InMemoryDecisionLog is falsy
        # (it defines ``__len__``), so ``decision_log or ...`` would silently drop
        # a caller-supplied empty log. Use an explicit ``is None`` check.
        decision_log=decision_log if decision_log is not None else InMemoryDecisionLog(),
    )


def _rbac_engine() -> AuthorizationEngine:
    from app.platform.authz.rbac import RbacEngine

    return RbacEngine(auth_role_catalogue())


def build_relation_graph(tuple_store: TupleStore | None = None) -> RelationGraph:
    """A standalone Kinora relation graph (for direct tuple-write workflows)."""
    store = tuple_store if tuple_store is not None else InMemoryTupleStore()
    return RelationGraph(KINORA_NAMESPACES, store)


__all__ = ["build_plane", "build_relation_graph"]
