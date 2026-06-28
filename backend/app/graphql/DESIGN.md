# Kinora Public GraphQL Gateway — `backend/app/graphql/`

> **Domain:** a stable, versioned **public API surface** separate from the internal REST
> routes (`app/api/routes/*`). It exposes the core Kinora domain — books, pages, shots,
> scenes/films, sessions, canon, directing prefs — over GraphQL, with API-key auth,
> per-key rate limits and scopes, persisted queries, depth/complexity limiting, cursor
> pagination, dataloader batching, error masking, a subscription bridge to the §5.6 event
> stream, schema export + a generated client SDK, and a deprecation/versioning policy.

This gateway is **additive and self-contained**: it mounts as its **own FastAPI
sub-application** at `/graphql` (registered from `app.api.routes.ROUTERS` — additive append
only) and **never edits** `app/api/routes/*` or `app/api/realtime/*`. It reuses the
existing `Container`, repositories, JWT/security and Redis, but introduces no schema
migration (API keys live in Redis), keeping the work migration-free and parallel-safe.

## Why hand-rolled (no new heavy dependency)

`strawberry`/`graphene`/`ariadne` are large dependencies needing build-script approval
churn in a 10-agent parallel monorepo. Per the task's "dependency-light hand-rolled
approach" guidance, this package ships a **small but real GraphQL engine**: a tokenizer +
recursive-descent parser for the query language, a type system (objects, scalars, enums,
lists, non-null, input objects, interfaces/unions), a validator
(depth/complexity/field-existence/argument checks), and an async executor with per-request
dataloaders. No third-party GraphQL lib; only stdlib + the existing backend.

## Module map

| Module | Responsibility |
|---|---|
| `language/lexer.py` | Tokenize GraphQL source |
| `language/ast.py` | AST node dataclasses |
| `language/parser.py` | Recursive-descent parser → Document AST |
| `language/printer.py` | AST → canonical GraphQL string (used in tests/tools) |
| `type_system.py` | GraphQLObject/Scalar/Enum/List/NonNull/InputObject/Field/Argument |
| `scalars.py` | ID/String/Int/Float/Boolean + DateTime/JSON/Cursor |
| `schema.py` | Schema container, type registry, introspection roots |
| `execute.py` | Async executor (query/mutation), field resolution, error collection |
| `validate.py` | Depth limit, complexity/cost limit, field/arg validation |
| `errors.py` | GraphQLError + masking policy |
| `dataloader.py` | Batching + per-key cache to kill N+1 |
| `pagination.py` | Relay-style cursor connections (opaque cursors) |
| `introspection.py` | `__schema`/`__type` resolvers + SDL printer |
| `auth.py` | API-key model (Redis), scopes, per-key token-bucket rate limit |
| `context.py` | Per-request execution context (container, key, loaders, user) |
| `types/` | The Kinora domain object types + enums |
| `resolvers/` | Field resolvers over the existing repositories |
| `root.py` | Query + Mutation + Subscription root field wiring |
| `persisted.py` | Persisted-query registry (sha256) + APQ flow |
| `subscriptions.py` | SSE bridge over Redis pub/sub → GraphQL subscription frames |
| `app.py` | The mountable FastAPI sub-app (`/graphql`, `/graphql/schema`, `/graphql/sdk`) |
| `sdk.py` | Generated TypeScript client SDK emitter (typed operations) |
| `versioning.py` | API version + deprecation policy surface |

## Schema (v1) — high level

```
type Query {
  apiVersion: ApiVersion!
  viewer: Viewer            # the API-key's owning account
  node(id: ID!): Node       # global object lookup
  book(id: ID!): Book
  books(first: Int, after: Cursor, status: BookStatus): BookConnection!
  shot(id: ID!): Shot
  session(id: ID!): Session
}
type Mutation {
  createReadingSession(input: CreateReadingSessionInput!): Session!
  updateIntent(input: UpdateIntentInput!): IntentResult!
  seek(input: SeekInput!): SeekResult!
  directorComment(input: DirectorCommentInput!): CommentResult!
  editCanon(input: EditCanonInput!): CanonEditResult!
  resolveConflict(input: ResolveConflictInput!): ConflictChoiceResult!
}
type Subscription { sessionEvents(sessionId: ID!): SessionEvent! }
```

Connections: `books`, `Book.shots`, `Book.scenes`, `Book.pages`, `Scene.shots`.
Field resolvers dataloader-batch shot/scene/book reads.

## Auth, scopes, rate limits

- **API key** = `kinora_pk_<id>.<secret>`; only a SHA-256 of the secret is stored
  (Redis hash `kinora:gql:key:<id>`), with `user_id`, `scopes`, `rpm`, label, created_at.
- Header `X-API-Key` (or `Authorization: Bearer kinora_pk_...`).
- Scopes: `books:read`, `sessions:read`, `sessions:write`, `canon:read`, `canon:write`,
  `director:write`, `prefs:read`. Mutations/fields declare required scopes; missing scope
  → masked `forbidden` GraphQL error.
- Per-key token bucket in Redis (same atomic-Lua approach as `api/deps.py`), default
  120 rpm, overridable per key.
- Keys provisioned by an authenticated end-user via `POST /graphql/keys` (JWT-authed,
  reuses `decode_access_token`).

## Limits

- **Depth limit** (default 12): reject deeply nested queries pre-execution.
- **Complexity/cost limit** (default 1000): each field has a `cost`; list fields multiply
  by `first`; summed cost over the limit → rejected.
- **Persisted queries**: clients may send `{ "id": "<sha256>" }` instead of `query`;
  unknown id → `PersistedQueryNotFound`. APQ handshake via
  `extensions.persistedQuery.sha256Hash` + register-on-miss.
- Max alias / total-node guards (DoS).

## Versioning / deprecation

- `Query.apiVersion` returns `{ version, deprecations[] }`.
- Fields carry deprecation reasons; the SDL printer + introspection surface them.
- `versioning.py` is the single registry of deprecations and the gateway semver.

## Testing

- Pure-unit (no infra): lexer, parser, printer, validator, scalars, type-system,
  pagination cursors, complexity, persisted-query hashing, SDL printer, SDK emitter, auth
  key hashing/scopes. These run everywhere.
- Infra-bound (skip cleanly without `KINORA_TEST_*`): end-to-end execution over the ASGI
  sub-app against throwaway Postgres/Redis/MinIO, mirroring `tests/conftest.py`.

## Additive shared-file changes (worktree rules)

- `app/api/routes/__init__.py`: append the gateway router/mount (additive only).
- No edits to existing routers, realtime, `main.py`, `composition.py`, `config.py`.

## Roadmap (phases)

1. ✅ Language core (lexer/ast/parser/printer) + type system + scalars.
2. ✅ Schema + executor + validator + errors + dataloader + pagination.
3. ✅ Domain types + resolvers over repositories; Query/Mutation/Subscription roots.
4. ✅ Auth (API keys/scopes/rate limit) + persisted queries + complexity.
5. ✅ Introspection + SDL export + TS SDK generator + versioning.
6. ✅ The mountable sub-app + SSE subscription bridge.
7. ✅ Tests (unit + infra-bound) and lint/type clean (ruff + mypy green; 82
   gateway unit tests + 10 infra-bound e2e).
8. ✅ Query batching (`compat.py` + array POST body); schema diff/compat checker
   (`compat.py`, breaking/dangerous/safe classification).

## Status — shipped

- Hand-rolled GraphQL engine: lexer → parser → AST printer; code-built type
  system (objects/interfaces/unions/enums/scalars/inputs + List/NonNull); async
  executor with spec error-bubbling, `@skip`/`@include`, fragments, aliases,
  `__typename`; validator (field/arg existence, leaf/composite, fragment cycles,
  **depth** + **complexity/cost** limits, node/alias guards).
- Public schema (56 named types): `Book`/`Page`/`Shot`/`Scene`/`Canon`/`Session`
  + `Node` global-id interface, Relay cursor connections, `apiVersion`/`viewer`,
  6 mutations (sessions, director comment, canon edit, conflict resolve),
  `sessionEvents` subscription bridged over SSE.
- API keys (`kinora_pk_<id>.<secret>`, sha256-hashed in Redis) + scopes +
  per-key Redis token-bucket rate limit (cost-weighted), provisioned via a
  JWT-authed `/graphql/keys` admin surface.
- Persisted queries + APQ, error masking, dataloader N+1 batching, SDL export
  (`/graphql/schema`), generated TypeScript SDK (`/graphql/sdk`), built-in
  playground (`/graphql`), versioning + deprecation registry, schema-compat diff.

## Additive shared-file changes (made, all additive)

- `app/api/routes/__init__.py`: added `root_routers()` returning the GraphQL
  router (lazy import; existing `ROUTERS` untouched).
- `app/main.py`: added a second mount loop `for router in root_routers(): app.include_router(router)`
  (root-mounted `/graphql`; the existing `/api` mount loop is untouched).
- No edits to any `app/api/routes/*.py` route module, `app/api/realtime/*`,
  `composition.py`, `core/config.py`, or `api/schemas.py`.

## Bug found + fixed while building

- Executor **double-coerced** an argument supplied as a whole-operation variable
  (it was coerced once at the variables stage, then re-coerced as a field arg),
  which broke enum inputs (an enum's *internal* value failed re-parse against its
  *public* names). Fixed in `execute.py::_coerce_field_args` (skip re-coercion for
  a bare `Variable` arg); regression-tested in `test_graphql_engine.py`.

## Remaining roadmap (future depth)

- Operation allow-listing mode (strict persisted-query-only) toggled per key.
- `@defer`/`@stream` incremental delivery.
- Field-level auth beyond scopes (row policies surfaced as directives).
- Real cursor pagination pushed into SQL (keyset) for very large shot lists.
- Wire `Book.directingStyle` (§8.6 prefs) + `Book.events`/film fields (§9.6).
