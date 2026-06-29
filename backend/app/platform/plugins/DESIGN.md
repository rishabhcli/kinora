# Sandboxed Plugin / Extension Platform (`app.platform.plugins`)

A self-contained extensibility platform that lets first- and third-party code
extend Kinora at four typed seams — **ingest filters, custom agents, render
post-processors, and webhook actions** — *without ambient authority*. The design
goal mirrors the rest of Kinora's platform layer (cf. `app/flags`): a **pure,
deterministic core usable with zero infrastructure**, wrapped by optional
Postgres persistence (the marketplace/registry), a capability-gated host-API
broker, and an admin/marketplace REST API.

It maps to kinora.md §6 (the agent crew is "stateless agents over shared canon …
swapped one at a time") and §8.3 (the MCP tool surface): a plugin is exactly that
— an independently versioned unit that reaches the canon/render/episodic surfaces
*only* through capabilities the host grants it.

## Design principles

1. **Deny by default / least privilege.** An empty `GrantSet` permits nothing.
   A capability is permitted only if a grant in the set *covers* it (hierarchical
   dotted scopes: `canon` covers `canon.read`, `canon.read` does not cover
   `canon.write`). The broker checks the grant **before** any host side effect, so
   a denied capability has no observable effect beyond the `CapabilityDeniedError`.
2. **Pure, infra-free core.** `capabilities`, `version`, `manifest`, `hooks`,
   `limits`, `lifecycle`, `resolver`, `marketplace`, `signing`, `runtime`,
   `broker`, `registry` import nothing from the DB/network. The sandbox is an
   in-process restricted interpreter you can drive with no DASHSCOPE key, no
   Postgres, no Redis — which is what makes the security tests deterministic.
3. **The manifest is the contract.** A plugin ships a declarative, validated
   manifest (identity, requested capabilities, hooks, dependencies, resource
   limits, import allowlist). Parsing never executes plugin code; a manifest that
   passes `PluginManifest.parse` is structurally trustworthy.
4. **Additive on shared files.** New ORM imported in `db/models/__init__.py`, a
   new router appended to `ROUTERS`, a new Alembic revision (`plugins_0001`) on an
   existing head. No edits to other agents' code.
5. **Fail closed.** Missing host service ⇒ denied. Missing signature when required
   ⇒ rejected. Unknown capability in a manifest ⇒ rejected at authoring time. A
   flapping plugin trips a circuit breaker into `QUARANTINED`.

## Module map

| Module | Responsibility | Infra |
|---|---|---|
| `errors.py` | Typed error hierarchy (authoring vs. sandbox/runtime) | none |
| `capabilities.py` | Hierarchical scopes, risk-tagged catalog, deny-by-default `GrantSet` | none |
| `version.py` | SemVer 2.0.0 + range matching (`^`, `~`, `x`, comparators) | none |
| `limits.py` | `ResourceLimits` budget value object + `clamp_to` ceiling | none |
| `hooks.py` | `ExtensionPoint` / `HookKind` taxonomy + `HookSpec` | none |
| `manifest.py` | Validated plugin descriptor (`PluginManifest`, `Dependency`) | none |
| `runtime.py` | The sandbox: restricted imports, safe builtins, resource budgets | none |
| `broker.py` | The capability-gated `HostAPI` — the only door to host power | none |
| `registry.py` | In-memory typed hook registry + deterministic dispatcher | none |
| `resolver.py` | Dependency resolution: ranges, closure, conflicts, topo-sort | none |
| `lifecycle.py` | install/enable/disable/upgrade/rollback/quarantine state machine | none |
| `marketplace.py` | Review state machine + rating aggregation + listing | none |
| `signing.py` | Content digest + detached HMAC signatures (fail-closed verify) | none |
| `db_models.py` | 5 ORM tables (`plugin_registry/installation/review/rating/audit`) | Postgres |
| `store.py` | Async repositories (flush-not-commit, UoW-owned tx) | Postgres |
| `service.py` | Orchestration facade (publish/review/rate/install/upgrade/dispatch) | Postgres |
| `api.py` | `/plugins` REST surface (self-contained, per-request service) | FastAPI |

## The sandbox guarantees (threat model: a buggy or hostile plugin author)

The runtime (`runtime.py`) executes plugin source in a namespace with **no
ambient authority** and enforces budgets:

1. **Restricted imports.** A gated `__import__` is injected into the plugin's
   builtins. Only modules on the effective allowlist (a conservative stdlib base
   set ∪ the manifest's declared list) are importable; a host **denylist**
   (`os`, `sys`, `subprocess`, `socket`, `io`, `pathlib`, `importlib`, `ctypes`,
   `app`, network modules, …) always wins, even if a manifest requests them.
   A forbidden `import` raises `ForbiddenImportError` at the import statement —
   at *load* time for a top-level import, at *call* time for a deferred one.
2. **No dangerous builtins.** `open`, `eval`, `exec`, `compile`, `__import__`
   (the raw one), `input`, `breakpoint`, `globals`, `vars`, `locals`, `memoryview`
   are removed from the plugin's `__builtins__`. So even with zero imports a
   plugin cannot reach the filesystem or the host frame.
3. **Resource budgets.** Wall-time (worker-thread timeout), host-call count,
   log-line count, and output-byte size are metered; exhausting any raises
   `ResourceLimitError` tagged with which limit tripped. A manifest's requested
   limits are `clamp_to`-ed against the operator ceiling, so a manifest cannot
   widen its own budget.
4. **Capability gating.** The injected `host` broker is the only capability-bearing
   object in scope. Every `host.call(scope, …)` checks the `GrantSet` first; a
   denied scope raises before the underlying host function is touched.

`tests/platform_plugins/test_sandbox_security.py` proves all of the above
deterministically: a denied capability raises *before* an in-memory recorder host
function runs (asserted by `recorder.calls == []`); forbidden imports, absent
builtins, file opens, and budget exhaustion all raise the right typed error.

## Extension points (the typed seams)

| Point | Kind | Composition |
|---|---|---|
| `ingest.filter` | TRANSFORM | fold — each hook transforms the running payload (a filter pipeline over §9.1 ingest beats) |
| `agent.custom` | PRODUCE | map — a plugin-provided agent step returning a typed response (§7 contract shape) |
| `render.postprocess` | PRODUCE | map — annotate/inspect a rendered shot artifact (§9.7), never mutate pixels |
| `webhook.action` | OBSERVE | for-effect — fire-and-forget reaction to a platform event (requires `net.fetch`) |

Hooks dispatch in deterministic `(priority, plugin_id, hook_id)` order. A hook
that raises is **isolated**: by default the dispatcher records the failure and
continues with the remaining hooks (one bad plugin cannot break the pipeline);
`fail_fast` re-raises for tests/trusted pipelines.

## Lifecycle & marketplace

- **Lifecycle** (`lifecycle.py`): `INSTALLED → ENABLED ⇄ DISABLED`, `UPGRADING`
  (transient), `QUARANTINED` (circuit breaker after N runtime failures),
  `UNINSTALLED` (terminal). A version ledger backs deterministic `rollback`.
- **Dependency resolution** (`resolver.py`): highest-version-in-range candidate
  selection, transitive closure, version-conflict detection, cycle detection +
  Kahn topological order (deps before dependents). Optional deps are skipped when
  unsatisfiable; missing required deps raise.
- **Marketplace** (`marketplace.py` + `store.py`): publish (idempotent on content
  digest, immutable versions), a review state machine
  (`PENDING → APPROVED/REJECTED/CHANGES_REQUESTED`, `APPROVED → YANKED`), 1–5★
  ratings (one per user, re-rating upserts), and install/rating counters.
- **Signing** (`signing.py`): content-addressed SHA-256 artifact digest +
  detached HMAC-SHA256 signatures, constant-time fail-closed verification. The
  `sign`/`verify` surface is identical to what an Ed25519 upgrade would expose.

## Additive shared-file changes (this package only)

- `app/db/models/__init__.py` — imports the five plugin ORM rows so Alembic
  autogenerate + `create_all` register them on `Base.metadata`; adds their names
  to `__all__`. (Inserted after the flags block, before the side-effect-only
  compliance import — that ordering is preserved.)
- `app/api/routes/__init__.py` — imports `plugins_router` and appends it to
  `ROUTERS` (mounted at `/api/plugins`). Existing routers keep their positions.
- `migrations/versions/plugins_0001_plugin_platform.py` — a new, reversible
  revision (UNIQUE id `plugins_0001`) creating the five tables. Chains the
  existing head `s1a2b3c4d5e6`; the marathon's final merge reconciles the
  multiple parallel heads created by sibling agents.

No `composition.py` edit is required: the `/plugins` router builds a
`PluginService` per request from `container.session_factory` (the committing
unit-of-work), reading optional host policy / signer / host-services factory off
the container via `getattr` (so a deploy that wires them gets them, and one that
doesn't fails *closed* — every host capability denied).

## Wiring the host (optional, when the platform is turned on)

A composition root can expose, on the container:

- `plugin_platform_config: PluginPlatformConfig` — signing requirement, auto-approve
  policy, resource ceiling, max grantable risk, quarantine threshold;
- `plugin_signer: Signer` — publisher key store for signature verification;
- `plugin_host_services_factory: (owner, plugin_id) -> HostServices` — the per-plugin
  bundle of capability-keyed host callables that back `canon.query`, `storage.kv.*`,
  `net.fetch`, `log.write`, etc. Each is scoped to the installing tenant + plugin.

Until wired, the platform persists and dispatches with an *empty* host-services
bundle (every brokered capability denied), which is the safe default.

## Tests

- `test_sandbox_security.py` — the security proof (capability denial, import
  allowlist, builtins, resource budgets, exception sanitization).
- `test_capabilities.py` / `test_version.py` / `test_manifest.py` /
  `test_resolver.py` / `test_lifecycle.py` / `test_signing.py` /
  `test_registry_dispatch.py` / `test_marketplace_policy.py` — pure-core units.
- `test_broker.py` / `test_runtime_more.py` — broker + runtime edge cases.
- `test_store_integration.py` — DB-backed publish/review/rate/install/upgrade/
  rollback/quarantine + end-to-end dispatch against an **isolated** Postgres
  (`KINORA_PLUGINS_TEST_DATABASE_URL`, e.g. `plugins_test` on :5433); skips when
  unset.
