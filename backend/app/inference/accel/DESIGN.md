# `app.inference.accel` — inference acceleration (DESIGN / roadmap)

> Facet **B** of the Kinora inference gateway. A **new, self-contained** package
> that makes LLM generation cheaper and faster *without changing its output*. It
> wraps an underlying `InferenceBackend` (facet A's transport / the DashScope
> `ChatProvider`) and layers speculative decoding, a semantic response cache,
> prefix/KV-reuse bookkeeping, multi-provider fan-out racing, and constrained
> decoding on top of it.

Reads: `kinora.md` §11 (model stack & budget — *spend tokens to save
video-seconds*), §12.3 (caching layers), §12.5 (observability), §10 (the agents'
strict JSON contracts that constrained decoding enforces).

## Why this exists

§11's binding rule is **"spend tokens to save video-seconds"**, and the agents
are token-heavy (Phase-A page analysis, planning, continuity, re-scoring). This
layer attacks the *token* cost and the *latency* of those calls so more of the
budget and the reader's patience survive for the scarce video stage:

- **Semantic cache** — a re-read, a paraphrased prompt, or an unchanged shot after
  a Director edit costs **zero** model tokens (the §8.7/§12.3 "request-level
  dedup" + "canon-embedding" ideas generalised to text generations).
- **Speculative decoding** — a cheap draft proposes, the expensive target only
  verifies, so each target call commits several tokens. Output is provably
  identical to plain target decoding.
- **Prefix/KV reuse** — the shared system/canon scaffold across agent calls is
  computed once; later calls reuse its KV blocks.
- **Fan-out racing** — first-good-wins across providers with a hard **cost cap**,
  so a flaky/slow primary cannot stall the pipeline and a race cannot overspend.
- **Constrained decoding** — guarantees the §10 JSON contracts parse, with a
  token-mask projection (for engines that can mask) and a bounded model-agnostic
  repair loop (for those that cannot).

This is **distinct** from `app.cache` (a general app cache) and
`app.providers.resilience` (transport retries/breakers/hedging at the HTTP
layer): accel reasons about *generation semantics* (tokens, embeddings,
schemas), not bytes or sockets.

## Architecture (all under `backend/app/inference/accel/`)

```
                            AcceleratedGateway (gateway.py)
        ┌──────────────────────────┴───────────────────────────┐
        │   generate():  cache → prefix-reuse → speculate/base → store
        │   race():      fan-out first-good-wins (orthogonal)
        │   generate_constrained(): schema + bounded repair (orthogonal)
        └──────────────────────────┬───────────────────────────┘
   ┌───────────┬───────────┬───────┴────┬─────────────┬──────────────┐
SemanticCache Speculative KVReuseBook  FanOutRacer ConstrainedDecoder  adapters
(semantic_   Decoder      (prefix_     (fanout.py) (constrained.py)   (adapters.py)
 cache.py)   (speculative (reuse.py)                                  ChatBackend /
             .py)                                                     EmbeddingAdapter
        └──────────── protocol.py (InferenceBackend / DraftBackend /
                       TokenScorer / Embedder + GenerationRequest/Result)
        clock.py · errors.py · metrics.py · tokenize.py · calibration.py · fakes.py
```

| Module | Responsibility |
|---|---|
| `protocol.py` | The consumed contract: `InferenceBackend`, `DraftBackend`, `TokenScorer`, `Embedder` Protocols + frozen `GenerationRequest`/`GenerationResult`/`TokenProposal`. |
| `clock.py` | `Clock` protocol, `SystemClock`, **`FakeClock`** — the deterministic time seam for every latency/TTL/hedge decision. |
| `errors.py` | `AccelError` family (`FanOutExhaustedError`, `CostCapExceededError`, `ConstrainedDecodeError`, `SpeculationConsistencyError`, `CalibrationError`). |
| `metrics.py` | Thread-safe per-component snapshots: acceptance rate, cache hit-rate, fan-out wins/cost, KV reuse rate. |
| `tokenize.py` | Vocabulary-agnostic word tokenizer + longest-common-prefix (shared by speculative accept + prefix reuse). |
| `speculative.py` | `SpeculativeDecoder` (draft-propose / target-verify, longest-accepted-prefix + bonus token) + `AdaptiveDraftLength` (AIMD controller on the running acceptance rate). |
| `semantic_cache.py` | `SemanticCache` — exact-prefix + embedding-similarity lookup, calibrated threshold, namespace **versioning** + TTL **staleness**, LRU eviction, temperature gate, hit-rate metrics. |
| `calibration.py` | `calibrate_threshold` — labelled prompt pairs → the cosine threshold meeting a target precision (max recall). |
| `prefix_reuse.py` | `PrefixTrie` (ref-counted) + `KVReuseBook` (paged-block accounting, capacity + LRU eviction, reuse-rate metrics). |
| `fanout.py` | `FanOutRacer` — first-good-wins racing, **cost cap**, staggered **hedging**, answer **validator**, loser cancellation. |
| `constrained.py` | `Constraint` hierarchy (choice / regex / JSON-type / JSON-schema) with `validate` + token-mask `allowed_next`; `ConstrainedDecoder` bounded repair loop. |
| `batching.py` | `RequestCoalescer` (single-flight dedup of concurrent identical generations) + `MicroBatcher` (accumulate → one §11 batch-API call, ~50% off). |
| `gateway.py` | `AcceleratedGateway` — composes the layers into one `InferenceBackend`; each layer optional/injectable. |
| `adapters.py` | `ChatBackend` / `EmbeddingAdapter` — the lazy seam from the DashScope `ChatProvider`/`EmbeddingProvider` to the accel protocols (no eager `httpx`/`dashscope` import). |
| `fakes.py` | Deterministic, network-free backends: `ScriptedTarget` (oracle, both `generate` + `verify`), `ScriptedDraft`, `HashEmbedder`, `GatedBackend` + `ManualClock` (event-driven race tests), `CountingBackend`, `StaticBackend`. |

## Key design decisions

- **Correctness is the contract, not a hope.** `SpeculativeDecoder.decode(req)`
  returns *exactly* `target.generate(req)` for any draft — proven by tests with a
  perfect, partial, useless, and 50-case fuzzed draft. The orchestrator also
  asserts the target never contradicts a committed token
  (`SpeculationConsistencyError`).
- **Injected clock everywhere.** No TTL, hedge delay, or latency path reads wall
  time directly; tests advance a `FakeClock`/`ManualClock`. Fan-out hedging is
  event-driven in tests, so there are **zero `sleep`s** in the suite.
- **Calibrated, not guessed, thresholds.** The semantic cache's similarity cut is
  derived from labelled pairs at a target precision; below it a neighbour is a
  *near-miss* (telemetry) and the lookup misses — a wrong cached answer is worse
  than a miss.
- **Versioning over scanning.** Bumping a namespace version invalidates all its
  entries at O(1) (the §12.3 "entity + version" pattern); TTL covers wall-time
  staleness.
- **Cost cap is a hard gate.** A fan-out only *starts* a candidate if committed +
  its cost stays within the cap; it never queues unbounded spend, mirroring the
  §11 budget discipline.
- **Self-contained + deterministic.** No live model calls anywhere in the package
  or its tests — works with `DASHSCOPE_API_KEY=test`, `KINORA_LIVE_VIDEO` off,
  zero credits.

## What this package did NOT touch

Purely additive. No existing module was edited. The provider transport
(`app.providers`) is consumed only through the lazy adapters in `adapters.py`,
which import it inside methods so importing this package never drags in
`httpx`/`dashscope`. Facet A's `InferenceBackend` (if/when present) is consumed
via the `protocol.py` shape; a one-line adapter bridges any signature drift.

## Test map (`backend/tests/inference/`)

| Test file | Covers |
|---|---|
| `test_accel_foundation.py` | clock, request/result value objects, metrics, tokenize, fakes |
| `test_accel_speculative.py` | equivalence (perfect/partial/useless/fuzzed drafts), adaptive `k`, consistency guard |
| `test_accel_semantic_cache.py` | exact + semantic hits, threshold, versioning, TTL, LRU, temperature gate, hit-rate |
| `test_accel_calibration.py` | threshold selection, precision/recall trade-off, unsatisfiable targets |
| `test_accel_prefix_reuse.py` | trie LCP + refcount + pruning, block accounting, capacity LRU |
| `test_accel_fanout.py` | first-good-wins, cost cap, hedging, validation, failure fall-through |
| `test_accel_constrained.py` | constraints, mask projection, repair loop |
| `test_accel_gateway.py` | composed cache/speculation/prefix layers + provider adapter seam |
| `test_accel_batching.py` | single-flight coalescing + micro-batching, flush + error fan-out |

## Roadmap (not yet built — left for a follow-up phase)

- **DI wiring** into `app.composition` so agents opt into the gateway (additive;
  intentionally deferred to avoid touching the composition root mid-marathon).
- **Settings** (cache size/TTL, default thresholds, speculative `k` bounds) in
  `app.core.config` once the package is adopted.
- **Persistent semantic cache backend** behind the same `SemanticCache` API
  (pgvector ANN / Redis) for cross-process sharing — the interface is ready.
- **Prometheus export** of the metric snapshots via `app.observability`.
- **Real draft/target wiring** to a small + large Qwen pair for live speculation.
