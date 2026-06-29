# `app/distributed` — the service-decomposition layer (internal RPC + mesh)

> Owner: distributed-systems agent (facet A: internal RPC + service mesh).
> Self-contained package under `backend/app/distributed/`. **Additive only** — it
> adds a layer *on top of* the existing monolith; it does not split or modify the
> ~40 working packages. Reads kinora.md **§6** (two-plane architecture; "every
> agent is an independently deployable service whose only shared dependency is the
> MCP memory server") and **§12** (the engineering discipline: idempotent,
> retried, dead-lettered, observable).

## The bet

kinora.md §6 makes an architectural *claim* — the crew are independently
deployable services over shared canon — but the backend ships as one FastAPI
process. This package makes the claim **true by construction without paying for a
physical split yet**: the existing packages are *addressed as logical services*
over a typed RPC seam that runs **in-process today** and can be flipped to a real
network transport **later, one service at a time, with no call-site change.**

The whole design turns on one seam, the `Transport`. Today a call is a direct
`await` into the target service's handler (zero copy). The day a service is split
out, only its discovery entry + transport binding change — the contract, the
typed stub, the resilience policies, and every call site stay byte-identical.

## Module map (`app/distributed/rpc/`)

| Module | Responsibility |
|---|---|
| `errors.py` | gRPC-shaped `RpcStatus` (wire-stable int codes, HTTP mapping) + `RpcError` with a **transport-vs-application** `FailureKind` and retryability classification. |
| `deadline.py` | Injectable `Clock` (`SystemClock` / deterministic `ManualClock`) + a **propagating `Deadline`** (absolute monotonic instant; `min_with` so a budget only shrinks down a chain — the §4.8 cancellation primitive). |
| `context.py` | `RequestContext`: deadline + **trace / correlation / auth / tenant / idempotency / baggage** propagation across hops, in a `contextvar`, bridged to `app.telemetry.context`. Wire (de)serialization via headers; `child()` = W3C-shaped span propagation with the inherited shrinking deadline. |
| `messages.py` | `RpcRequest` / `RpcResponse` envelopes (result-or-error in band, gRPC-trailer style). |
| `contracts.py` | The **typed in-Python IDL**: `ServiceContract` + `MethodSpec` + `method()`. Codec encodes/decodes pydantic models, dataclasses, dict, and scalars. `idempotent`/`streaming` flags carry real semantics. `fingerprint()` asserts two endpoints agree on the wire shape. |
| `transport.py` | The seam: `InProcessTransport` (zero-copy, the production default now), `LoopbackTransport` (JSON round-trip — proves **split-readiness**), `DeterministicFakeTransport` (tests; scriptable latency/faults/recording, never a socket). `RemoteTransportConfig` sketches the future HTTP/gRPC wiring point. |
| `registry.py` | `ServiceRegistry` (write side: register contracts + instances, heartbeat) + `Discovery` (read side: resolve → instances, TTL'd heartbeats, version filter) over a pluggable `RegistryStore` (in-memory now; Consul/etcd/Redis later behind the same protocol). |
| `health.py` | Active `HealthChecker` (timed component probes → worst-status rollup; critical vs degrade) + passive `OutlierDetector` (Envoy-style ejection inferred from the call stream). |
| `loadbalancer.py` | Round-robin, random, least-connections, **power-of-two-choices**, and **rendezvous (consistent) hashing** (session/`shot_hash` affinity for the §12.3 caches). Filters unhealthy instances; pure + deterministic. |
| `retry.py` | Exponential backoff + decorrelated jitter **and a shared `RetryBudget`** (token bucket capping retries as a fraction of primary traffic → no retry storms). Idempotent + retryable + deadline + budget gated. |
| `hedging.py` | "Tail at scale" hedged requests: race a backup after a hedge delay, take the first success, cancel the losers. Idempotent-only, hedge-budgeted, structured-concurrency (`anyio` task group). |
| `circuit.py` | Three-state circuit breaker (closed/open/half-open) with a rolling failure-ratio window; counts **only** transport/distress failures, never a healthy server's application error. Per-endpoint registry. |
| `client.py` | `RpcClient`: composes the full stack per call — deadline gate → retry(budget) → hedge(idempotent) → discovery → load-balance → circuit gate → transport → classify. Result-or-error `RpcResponse`. Per-call `CallOptions`. |
| `server.py` | `ServiceServer`: binds a contract to a plain impl object, rehydrates the context, enforces the deadline, decodes/encodes, **dedups by idempotency key** (§12.1 "re-enqueue is a no-op"), normalises errors, runs an interceptor chain. |
| `interceptors.py` | Reusable server interceptors: access log, require-auth, require-tenant, token-bucket rate limit (per-tenant fairness), concurrency bulkhead, audit ring. |
| `stub.py` | `ServiceStub`: typed runtime client stubs ("codegen") generated from a contract — call sites read like calling the object directly. `generate_stub_source()` emits an editor `.pyi`. |
| `mesh.py` | `ServiceMesh`: the **façade** the rest of the backend touches. `register(contract, impl)` + `stub(name)` / `call(...)`; health + `topology()`; `build_default_mesh` / `build_test_mesh` factories. |
| `catalog.py` | The concrete Kinora service contracts — cinematographer, generator, critic, memory, budget, scheduler, search — with real idempotency semantics. |
| `wiring.py` | `mount_catalog_services(mesh, …)`: the additive bridge the composition root *calls* (not an edit to it) to mount its existing collaborators as logical services. |

## Semantics / guarantees

- **In-process now, network-later, one call site forever.** Splitting a service
  out is a wiring change (its registry entry + transport), never a call-site
  change. `LoopbackTransport` is the CI gate that proves a service's
  request/response survive a real wire before it is split.
- **Deadlines propagate and shrink.** A chain can never collectively exceed the
  originator's deadline; an expired budget trips `DEADLINE_EXCEEDED` without doing
  work (server-side too). This is the §4.8 seek-cancellation primitive,
  generalized.
- **Retries can't amplify an outage.** The shared retry budget caps retries as a
  fraction of primary traffic; non-idempotent / non-retryable calls fail fast.
- **The breaker trusts the right signal.** Only transport / distress failures
  count; a healthy server's `NOT_FOUND` never trips it.
- **Idempotent writes dedup.** A duplicate Scheduler event / client retry carrying
  the same idempotency key collapses to one execution and replays the first
  result (§12.1) — the video budget can't be double-spent.
- **Deterministic under test.** Every temporal decision goes through the injected
  clock + sleep seam; the fake transport never opens a socket. The whole policy
  stack is exercised with a `ManualClock` — fast, flake-free, zero credits.

## Additive changes to shared files

**None.** This package adds only new files under `backend/app/distributed/` and
new `tests/test_distributed_rpc_*.py`. It *reuses* `app.telemetry.context`
(trace/correlation bridge) and *registers additive* `kinora_rpc_*` Prometheus
series against the existing shared `app.observability.metrics.registry` via
`app/distributed/rpc/metrics.py` — no edit to that module. No new DB tables, no
new migration. `wiring.py` is a helper the composition root may *call*; it does
not modify `app/composition.py`.

## Test surface

`tests/test_distributed_rpc_*.py` — 155 tests, no infra, zero network:
errors · deadline+context · contracts+codec · transport (3 kinds) · retry+budget ·
circuit · hedging · loadbalancer · registry+discovery · health+outlier ·
client (policy composition) · server (dispatch/dedup/authz) · interceptors ·
stub+codegen · mesh end-to-end + the Kinora catalog · wiring.

## Roadmap (built bottom-up, more buildable on top)

Shipped: the full in-process substrate + resilience policies + façade + catalog +
wiring. Natural next layers (each additive, behind the same seams):

1. A real `HttpxTransport` / `GrpcTransport` behind `RemoteTransportConfig` (the
   only socket-opening code; lives outside the test path).
2. A Redis/etcd-backed `RegistryStore` for cross-process discovery + heartbeats.
3. Server-streaming methods over the `streaming` contract flag (the §5.6 event
   channel as a first-class mesh stream).
4. A FastAPI mount that exposes `topology()` + `check_all()` at `/_mesh/*` for an
   operability dashboard.
5. Zone-aware + weighted load balancing (the `zone`/`weight` fields are already on
   `ServiceInstance`).
