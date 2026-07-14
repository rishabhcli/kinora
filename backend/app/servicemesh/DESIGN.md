# `app/servicemesh` — internal message/RPC contracts + schema versioning

Kinora's backend image runs as several **roles** off one codebase (AGENTS.md /
kinora.md §12 process model): `api`, `ingest-worker`, `render-worker`, `mcp`. They
communicate over three internal channels:

| channel | carrier | example messages |
|---|---|---|
| queue jobs | Redis priority queue | `shot.render.job` (idempotent by `shot_hash`, §12.1) |
| pub/sub events | Redis pub/sub fan-out | `shot.progress`, `buffer.state` (§5.3 hairline) |
| MCP calls | the canon-memory MCP server | `canon.query` / `canon.query.result` |

Because each role deploys **independently**, every message is a *contract*: producer
and consumer must agree on its shape and version, and that agreement has to survive
schema evolution without a flag-day redeploy. This package is that contract layer.

It is **additive and import-safe**: importing it opens no sockets, touches no DB,
and starts no event loop. It does **not** rewrite any existing queue-job / pubsub /
MCP message shape — it is a substrate those call sites can adopt incrementally.

## Layering (bottom → top)

```
errors            normalized failure taxonomy
versioning        SemVer + VersionRange (self-contained, no deps)
roles             ProducerRole / TransportKind / ContentType (§12)
schema            MessageSchema (structural) + deterministic content hash
envelope          MessageEnvelope: versioned, trace/correlation/idempotency frame
compatibility     change classification + backward/forward/full + the CI gate
registry          SchemaRegistry: register by semver + hash, gate on evolution
converters        ConverterRegistry: migrator graph → shortest-path up/down chains
consumer          ConsumerDispatcher: validate + route by (id,version), dead-letter
negotiation       capability/version handshake between two roles
catalog           the concrete Kinora message contracts + a seed registry
```

## Key design decisions

### Structural schemas, not full JSON-Schema documents
A `MessageSchema` is an ordered set of `FieldSpec`s (coarse type, required, nullable,
enum domain, array element type). This is enough for the compatibility checker to
reason field-by-field, and it yields a **stable content hash** (SHA-256 over a
canonical, key-sorted projection) that is invariant under field reordering and
cosmetic edits. `MessageSchema.from_model` derives one from a pydantic v2 model so an
existing DTO can be registered without re-typing its shape.

### The envelope is the unit of inter-service messaging
`MessageEnvelope` wraps an opaque payload mapping with: `schema_id` +
`schema_version` (the registry key), `content_type`, `transport`, `trace_id` /
`span_id`, `correlation_id`, `causation_id` (event-sourcing lineage),
`idempotency_key` (§12.1 dedupe), `emitted_at` (injectable clock → deterministic
tests), and `producer_role`. `caused_child()` propagates trace + correlation and
records causation, so a whole render flow (enqueue → worker → progress events) is
one correlated chain.

### Compatibility = per-change classification, folded
Each structural delta maps to a `ChangeKind` with a known `(backward, forward)`
verdict (see the table in `compatibility.py`). An evolution is backward-compatible
iff *every* change is; full = backward ∧ forward. The **CI gate**
(`assert_evolution_allowed`) raises `BreakingChangeError` when the classified change
violates the channel's declared mode on a *stable* channel — honouring two SemVer
conventions: pre-1.0 may break on a minor bump (`stable_only`), and a MAJOR bump is
a *declared* intentional break (always allowed). The registry runs this gate on
every evolution, so a breaking change can never be registered (and thus never
emitted) on a stable channel.

### Graceful version negotiation via a migrator graph
Rather than make every consumer handle every historical shape, we register
**adjacent** migrators (one version step, up or down) and compose them via BFS into
the shortest conversion chain. A `v1` producer and a `v3` consumer talk because the
dispatcher converts `v1→v2→v3` from two registered adjacent migrators. No path →
`NoConversionPathError`, which the dispatcher turns into a dead-letter.

### Consumer dispatch with a dead-letter, not a crash
`ConsumerDispatcher` binds handlers to `(schema_id, version)`. On an envelope it:
looks up the schema (unknown id → DLQ), resolves a handled version (exact, else
nearest reachable via conversion; none → DLQ), structurally validates the
(converted) payload (invalid → DLQ), then routes (handler raises → DLQ). Nothing
unhandlable crashes the worker loop — the §12 "dead-lettered render queue"
discipline applied to the *contract* layer. Handlers may be sync or async.

### Negotiation is a pure fold over capabilities
A role advertises, per `schema_id`, the version ranges it can *produce* and
*consume* plus a preference. `negotiate` intersects produce ∩ consume and picks the
highest commonly-supported version (preferring the producer's preference when it
lies in the overlap), or raises `NegotiationError` on disjoint ranges.

## Settings
`ServiceMeshSettings` (env prefix `SERVICEMESH_`) is **local to this package** — not
folded into `app/core/config.py` — to keep the subsystem additive. Safe defaults:
`default_compatibility=backward`, `enforce_gate=true`, `stable_only=true`,
`validate_payloads=true`.

## Tests
`tests/servicemesh/` — fully deterministic, no infra, no network, no
`KINORA_LIVE_VIDEO`: envelope round-trip + lineage, registry + content-hash +
idempotent re-registration, compatibility classification catching each break class,
the CI gate (reject breaking on stable / allow on MAJOR / allow pre-1.0), converter
chain up/down (multi-hop + no-path), consumer dispatch + every dead-letter reason,
and capability/version negotiation.
