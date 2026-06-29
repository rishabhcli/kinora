# Unified Authorization Plane — DESIGN.md (living roadmap)

Owner domain (Agent: Platform engineering, facet C — unified authorization).
NEW package `backend/app/platform/authz/`. Folds Kinora's scattered
ownership/permission checks behind ONE plane via **behaviour-preserving
adapters** — it does **not** rip the existing checks out.

## The problem it solves

Kinora grew authorization in four unrelated places, with no shared vocabulary,
no unified audit, and no single "may this subject do this?" entry point:

| Existing check | What it does | Folded in by |
|---|---|---|
| `app.auth.rbac` (`Principal`, `has_capability`) | RBAC roles + API-key scopes, `*`/`ns:*` wildcards | `AuthRbacAdapter` (pure) + native `RbacEngine` over the same `ROLES` catalogue |
| `app.workspaces.authz.AuthorizationService.can` | DB-backed personal-owner / workspace-membership / org-owner / direct-share grant resolution | `WorkspaceAuthzAdapter` (async) |
| `app.mcp.authz.BookScopedAuthorizer` | reject MCP calls naming an unknown `book_id` | `McpBookScopeAdapter` (async) |
| `app.moderation.policy.evaluate` | content disposition (ALLOW/FLAG/BLOCK) | `ModerationPolicyAdapter` (pure; BLOCK→DENY) |

Each adapter **delegates to the original check**, so the legacy decision is
preserved exactly while becoming expressible, cacheable, and auditable through
one `check(subject, action, resource, context)`.

## Architecture (all pure unless noted)

- `model.py` — the request document (`Subject`/`Action`/`Resource`/`Context`),
  three-valued `Effect` (ALLOW/DENY/**ABSTAIN**), `Decision` + structured
  `Reason` trail + `Obligation`s, stable `cache_key`.
- `engine.py` — the `AuthorizationEngine` protocol (sync `evaluate` /
  async `aevaluate`); `SyncEngine` base.
- `combining.py` — XACML combining algorithms: `DENY_OVERRIDES` (default),
  `PERMIT_OVERRIDES`, `FIRST_APPLICABLE`, `DENY_UNLESS_PERMIT`. Three-valued so
  ABSTAIN (no opinion) is distinct from DENY.
- `rbac.py` — `RbacEngine` + `RoleCatalogue` reusing the legacy `ROLES` + exact
  `has_capability` wildcard matching (verified by a parity test).
- `abac.py` — `AbacEngine` + a composable, side-effect-free `Condition` algebra
  (`Attr`, `AttrEqAttr`, `AllOf`/`AnyOf`/`Not`, `is_owner`, `same_tenant`) shared
  with the DSL via `resolve_attr`.
- `dsl.py` — a **Rego/OPA-style** policy DSL: parser → `Policy` AST →
  `evaluate_policy` (deny-overrides) → `PolicyEngine`. **Partial evaluation**
  (`partial_evaluate`) folds away known attributes and returns the residual
  unknown constraints (powers what-if + reverse-index pruning).
- `rebac.py` — the **Google-Zanzibar** relationship model: `RelationTuple`
  (`object#relation@subject`, incl. **userset** subjects), namespace
  **userset-rewrites** (`This`/`ComputedUserset`/`TupleToUserset`/`Union`/
  `Intersection`/`Exclusion`), `RelationGraph.check` (cycle-guarded) and the
  reverse-index `RelationGraph.list_objects` ("objects I can access"),
  `TupleStore` protocol + `InMemoryTupleStore`, `RebacEngine` (action→relation).
- `cache.py` — `DecisionCache` protocol + `InMemoryDecisionCache` (TTL + LRU +
  **tag invalidation** by subject/resource ref) + `NullDecisionCache`.
- `audit.py` — `DecisionLog` protocol + `InMemoryDecisionLog` (ring buffer,
  queryable) + `CompositeDecisionLog` + `DecisionRecord` (stable `digest`) +
  `summarize`.
- `sdk.py` — `AuthorizationPlane`: `check` / `check_sync` / `is_allowed` /
  `require` (raises `AccessDeniedError`) / `list_objects` / cache invalidation.
- `presets.py` — Kinora's namespaces (the workspaces role lattice as rewrites:
  org→workspace→book parent inheritance + OWNER⊃EDITOR⊃COMMENTER⊃VIEWER), the
  action→relation map, the default ABAC invariants (admin override, personal
  owner, tenant isolation).
- `testing.py` — `PolicySuite`/`PolicyTestCase` + **coverage** (which rules a
  suite exercised; flags dead policy).
- `simulation.py` — **what-if**: `diff_planes` / `would_change` /
  `scenario_grid` diff a candidate plane against the current one before rollout.
- `adapters.py` — the four behaviour-preserving adapters (above).
- `factory.py` — `build_plane(...)` one-call wiring; `build_relation_graph`.
- `db_models.py` + `store_db.py` — durable backing: `authz_relation_tuples` +
  `authz_decision_log`; `DbTupleStore` (snapshot-backed, same `TupleStore`
  protocol) + `DbDecisionLog` (buffered flush).

## Combining posture

Default `DENY_OVERRIDES`: an explicit DENY (tenant isolation, a fired policy
deny, a forged MCP book) beats any ALLOW. ABSTAIN = "no opinion" so a missing
role/relation never blocks an allow from another engine. Adapters emit ALLOW on
grant and **ABSTAIN** (not DENY) on no-grant — preserving the legacy
"most-permissive path wins" semantics — except the MCP/moderation adapters which
emit DENY for forged-book / blocked-content (their legacy hard-fail behaviour).

## Additive shared-file changes (per the parallel-agent rules)

- `app/db/models/__init__.py`: appended an import of
  `app.platform.authz.db_models` (`AuthzRelationTuple`, `AuthzDecisionLogRow`)
  and two `__all__` entries. Registration-only; touches no existing model.
- New Alembic migration `authzplane_0001` (UNIQUE domain-prefixed id), branching
  off the shared base `a1b2c3d4e5f6` as its own head — creates the two new tables
  only, no existing-schema change.

No other shared file is modified. `composition.py` is intentionally **not**
touched (the plane is constructed via `build_plane`; wiring a Container seam is a
follow-up that another agent's composition edits should not collide with).

## Tests (all green; pure suite needs no infra)

- `test_authz_model_combining.py` — request document, cache key, all four
  combining algorithms, obligation handling.
- `test_authz_rbac_abac.py` — RBAC parity with `auth.rbac`, ABAC condition
  algebra, attribute resolution, first-applicable + deny short-circuit.
- `test_authz_dsl.py` — parse/evaluate/partial-eval, deny-overrides, residuals.
- `test_authz_rebac_zanzibar.py` — every rewrite type, userset subjects, cycle
  protection, reverse-index ≡ forward check.
- `test_authz_rebac_consistency.py` — exhaustive sweep: `list_objects` ≡
  `{o : check(o)}` over a multi-hop (org→workspace→book) Kinora world.
- `test_authz_cache_audit.py` — TTL, LRU, tag invalidation, log queries, stats.
- `test_authz_testing_simulation.py` — suite pass/fail, coverage, what-if diff.
- `test_authz_adapters.py` — each adapter's parity with the wrapped legacy check.
- `test_authz_plane_integration.py` — the composed plane end-to-end (sync+async,
  cache hits, audit, list_objects, deny-overrides over RBAC wildcard).
- `test_authz_store_db.py` — DB-backed store ≡ in-memory store (isolated DB,
  `KINORA_AUTHZ_TEST_DATABASE_URL`; skips when unset). Verified against
  `authzplane_test` on :5433.

## Remaining roadmap (future, additive)

- Wire a `Container.authz_plane` seam in `composition.py` + a FastAPI dependency
  (`require_authz(action, resource)`) and migrate route guards onto it
  incrementally (each migration verified equivalent via `simulation.diff_planes`).
- Persist a backfill job that materialises existing workspace/share rows into
  `authz_relation_tuples` (so the native ReBAC engine can serve `list_objects`
  without the per-request workspace adapter round-trips).
- Redis-backed `DecisionCache` for multi-process invalidation; structured-logging
  `DecisionLog` sink.
- A read-time `tuple_to_userset` index materialisation for very large tenants.
