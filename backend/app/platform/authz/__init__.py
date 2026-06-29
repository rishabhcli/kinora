"""The unified authorization plane — one ``check(subject, action, resource, context)``.

Kinora grew authorization checks in many places: the auth package's RBAC
roles/scopes (:mod:`app.auth.rbac`), the workspaces collaboration model's
``can(user, action, resource)`` (:mod:`app.workspaces.authz`), the MCP tool
surface's book-scoping (:mod:`app.mcp.authz`), and the moderation policy
(:mod:`app.moderation.policy`). Each is correct in isolation but they share no
vocabulary, no audit trail, and no single place to ask "may this subject do
this?".

This package is that single place — an *authorization fabric* that folds those
scattered checks behind one plane **without changing their behaviour**:

* a **request document** (:mod:`~app.platform.authz.model`) — subject / action /
  resource / context, three-valued effects, structured explanations;
* an **RBAC engine** (:mod:`~app.platform.authz.rbac`) reusing the legacy role
  catalogue + wildcard matching, and an **ABAC engine**
  (:mod:`~app.platform.authz.abac`) of attribute conditions;
* a **Rego-style policy DSL** + evaluator with partial evaluation
  (:mod:`~app.platform.authz.dsl`);
* a **Google-Zanzibar-shaped** relationship model — tuples, userset rewrites, a
  check API, and a reverse-index "list objects I can access"
  (:mod:`~app.platform.authz.rebac`);
* **combining algorithms** (:mod:`~app.platform.authz.combining`) that fold the
  engines into one verdict (deny-overrides by default);
* a **decision cache** (:mod:`~app.platform.authz.cache`) and an append-only
  **decision log / audit** (:mod:`~app.platform.authz.audit`);
* **policy testing + coverage + simulation** ("what-if" before rollout)
  (:mod:`~app.platform.authz.testing`, :mod:`~app.platform.authz.simulation`);
* **adapters** (:mod:`~app.platform.authz.adapters`) that wrap the existing
  subsystems as plane engines so their behaviour is preserved exactly.

The public entry point is :class:`~app.platform.authz.sdk.AuthorizationPlane`
and its ``check`` / ``is_allowed`` / ``list_objects`` methods. The plane is
additive: nothing in the existing checks is removed; adapters delegate to them
so the migration is behaviour-preserving by construction.
"""

from __future__ import annotations

from app.platform.authz.audit import (
    DecisionLog,
    DecisionRecord,
    InMemoryDecisionLog,
    summarize,
)
from app.platform.authz.cache import (
    DecisionCache,
    InMemoryDecisionCache,
    NullDecisionCache,
)
from app.platform.authz.combining import CombiningAlgorithm, combine
from app.platform.authz.engine import AuthorizationEngine, SyncEngine
from app.platform.authz.factory import build_plane, build_relation_graph
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Decision,
    Effect,
    EngineResult,
    Obligation,
    Reason,
    Resource,
    Subject,
)
from app.platform.authz.sdk import AccessDeniedError, AuthorizationPlane

__all__ = [
    "AccessDeniedError",
    "AuthorizationEngine",
    "AuthorizationPlane",
    "AuthorizationRequest",
    "CombiningAlgorithm",
    "Context",
    "Decision",
    "DecisionCache",
    "DecisionLog",
    "DecisionRecord",
    "Effect",
    "EngineResult",
    "InMemoryDecisionCache",
    "InMemoryDecisionLog",
    "NullDecisionCache",
    "Obligation",
    "Reason",
    "Resource",
    "Subject",
    "SyncEngine",
    "build_plane",
    "build_relation_graph",
    "combine",
    "summarize",
]
