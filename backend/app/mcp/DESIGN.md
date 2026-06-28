# MCP canon server — protocol / transport layer (DESIGN.md)

Owner agent domain: `backend/app/mcp/server.py`, `run.py`, `skills.py`, plus the
new protocol/transport modules added under `backend/app/mcp/`. The execution
path for every tool is **`MemoryTools.dispatch`** (`tools.py`); `tools.py` and
`schemas.py` are owned by round-1 work and are **not rewritten** here — this
layer wraps them with a complete, spec-compliant MCP protocol surface.

Spec anchor: `kinora.md §8.3` (the MCP tool surface) + `§12` (the control-surface
auth story).

## Goal

Build a complete MCP server around the existing canon tools:

1. Full protocol compliance (initialize / list / call) — leverages the official
   `mcp` SDK `Server`, layered with our own metadata.
2. Tool **versioning** + JSON-Schema request/response **validation**.
3. **Capability negotiation** descriptor (what this server advertises).
4. **Resource** surface + **subscriptions** + change notifications.
5. **Auth + per-client scoping** (identity threaded from the bearer subject).
6. A typed **Python client SDK** (in-process + protocol round-trip).
7. A **conformance** test suite.

## Module map (all additive, owned by this agent)

| Module | Responsibility |
|---|---|
| `errors.py` | Typed MCP error taxonomy (JSON-RPC-aligned codes) + `to_tool_error`. |
| `registry.py` | `ToolCatalog`: wraps `TOOL_DEFS` with version, scope tags (read/write, book-scoped), output-model, resource bindings. Single source of tool metadata. |
| `validation.py` | `SchemaValidator`: validate request args + response payloads against the registry's JSON Schemas. |
| `capabilities.py` | `ServerCapabilities` descriptor + `negotiate()` against a client's declared capabilities. |
| `resources.py` | Canon resource URIs (`kinora://…`), a `ResourceProvider` reading them via `MemoryTools`, a `SubscriptionRegistry`. |
| `identity.py` | `ClientIdentity` (subject from bearer), `ClientScope` (per-client allow/deny), scope-enforcing authorizer composing with `BookScopedAuthorizer`. |
| `session.py` | `ClientSession` state + `SessionStore`. |
| `client.py` | `KinoraMCPClient` typed SDK — calls tools by name with typed in/out models; `InProcessTransport`. |
| `conformance.py` | Reusable conformance checks callable from tests and a CLI. |

`server.py` gains: capability advertisement, request/response validation wired
into `_call_tool`, resource list/read/subscribe handlers + change-notification
plumbing, identity-aware authorization. `run.py` exposes the richer build.

## Invariants honored

- `KINORA_LIVE_VIDEO` stays OFF; nothing here renders or spends credits.
- Single execution path: every tool call still funnels through
  `MemoryTools.dispatch`. The new layers validate / authorize / observe around it.
- Additive on shared files: `core/config.py` (new MCP settings, appended),
  `composition.py` (new builder methods, appended). No edits to `tools.py` /
  `schemas.py`.

## Additive shared-file changes

- `core/config.py`: `mcp_versioning_enabled`, `mcp_validate_responses`,
  `mcp_resource_subscriptions`, `mcp_client_scopes` (JSON map of token→scope).
- `composition.py`: `build_mcp_catalog()`, `build_mcp_resource_provider()`,
  `build_scoped_authorizer()` — appended methods, lazy.

## Roadmap / phases

- [x] Phase 0: study spec + existing code, baseline green.
- [x] Phase 1: `errors.py` + `registry.py` (versioning, scope tags, output schemas).
- [x] Phase 2: `validation.py` (request + response JSON-Schema validation).
- [x] Phase 3: `capabilities.py` (advertisement + `negotiate`).
- [x] Phase 4: `resources.py` (canon resources + subscriptions + change fan-out).
- [x] Phase 5: `identity.py` (identity resolver + scope/book authorizer + middleware).
- [x] Phase 6: `server.py` integration (`build_protocol_server`, validation,
      capabilities init options, resource handlers, version pins, identity gate).
- [x] Phase 7: `client.py` typed SDK (`InProcessTransport` + `SessionTransport`).
- [x] Phase 8: `session.py` store + `run.py` wiring (scoped authorizer + identity
      middleware when `MCP_CLIENT_SCOPES` configured).
- [x] Phase 9: conformance suite (`conformance.py`) + tests.

## Delivered (final state)

Modules added under `backend/app/mcp/`: `errors.py`, `registry.py`,
`validation.py`, `capabilities.py`, `resources.py`, `identity.py`, `session.py`,
`client.py`, `conformance.py`. `server.py` gained `ToolDispatcher`,
`build_protocol_server`, `ProtocolServer`, version-pin handling, resource
handlers, identity-aware HTTP build; `run.py` wires the full surface.

Tests (no infra): `test_mcp_registry.py`, `test_mcp_validation.py`,
`test_mcp_capabilities.py`, `test_mcp_identity.py`, `test_mcp_session.py`,
`test_mcp_protocol.py` (full in-memory client↔server round-trip),
`test_mcp_http_scoping.py`, plus additions to `test_mcp_run.py`.

`make lint` (ruff + mypy, 387 files) and `make test` (1119 passed / 160
infra-skipped) both green. Conformance CLI: `python -m app.mcp.conformance`
(15/15). `KINORA_LIVE_VIDEO` untouched; zero credits spent.

## Future roadmap (not yet built)

- Tool list-change notifications fired on a live catalog reload (the capability
  is advertised; no dynamic catalog mutation exists to trigger it yet).
- Per-session capability persistence in `SessionStore` keyed off the SDK session
  id over the streamable-HTTP transport (the store exists; the transport keys
  subscriptions on session-object identity today).
- A versioned tool *alias* mechanism (serve `canon.query` v1 and v2 side by side)
  once a tool's contract actually changes — `_VERSIONS` in `registry.py` is the
  single place to bump.
