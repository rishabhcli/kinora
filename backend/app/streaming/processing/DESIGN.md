# `app/streaming/processing` — stream processing (facet B)

> Owner: streaming-data-plane facet-B agent. Self-contained NEW package under
> `backend/app/streaming/processing/`. Pure, deterministic Python — no network,
> no DB, no model providers, no settings. Safe to import anywhere.

A **Flink / Kafka-Streams-shaped stateful stream processor**: a dataflow DAG of
operators, event-time windowing with watermarks and allowed-lateness, stateful
aggregations and joins, a checkpointed state backend with exactly-once
semantics, and concrete pipelines over Kinora's **reader-intent** stream (§4.3)
and **render-event** stream (§5.6) that feed the §13 metrics panel.

Reads kinora.md **§4** (reader events, scheduler, failure modes) and **§13**
(metrics & eval harness).

## Why a stream processor (and why here)

Kinora is two long-lived event streams — the reader's attention (intent updates,
seeks, idle-pauses) and the generation pipeline's output (keyframes, clips, QA
verdicts, budget). The dashboards the product and the demo need —
engagement/velocity analytics, render throughput, accept/regen rate, p50/p95
latency, CCS — are all **windowed, keyed, event-time aggregations over those
streams.** A purpose-built stream processor expresses them once, correctly,
under out-of-order delivery, instead of ad-hoc per-metric loops. This is facet
**B** of the streaming data plane; facet **A** (`app.streaming.log`) owns the
append-only log + `Broker` protocol this consumes from.

## Relationship to the existing `app.scheduler`

The §4.9 Scheduler is the **control plane** — it decides *what to render now*. It
is untouched. This package is **observability/analytics** over the same signals:
it never enqueues a render or spends a video-second. The two share concepts
(focus-word, velocity, watermark buffer) but not code; the stream processor
*measures* what the scheduler *does*.

## Components (modules)

| Module | Responsibility |
|---|---|
| `records.py` | `StreamRecord` (value + event-time + key), `Watermark`, `LatePolicy`, side-output tags. |
| `time_domain.py` | `WatermarkStrategy` (bounded-out-of-orderness / monotonous), timestamp assigners, the event-time `TimerService`. |
| `state.py` | Keyed state (`Value`/`List`/`Map`/`Reducing`/`Aggregating`), `Snapshot`, `CheckpointStorage`, `CheckpointCoordinator` — the **exactly-once** backbone. |
| `aggregations.py` | `AggregateFunction` / `ReduceFunction` + count/sum/min/max/mean/collect + `percentile`. |
| `windows.py` | Tumbling / sliding / session assigners; session **merge** primitive. |
| `triggers.py` | `EventTimeTrigger`, `CountTrigger`, `PurgingTrigger`, `TriggerResult`. |
| `operators.py` | `Operator` protocol; map/filter/flatMap/keyBy/split/union; `ProcessFunction` and `CoProcessFunction` (two-input keyed, shared state + timers + side outputs). |
| `window_operator.py` | The keyed event-time window operator: assignment, session merge, incremental aggregate, allowed-lateness re-fire + late side-output, cleanup timers. |
| `joins.py` | Stream-stream **interval join** + stream-table **enrichment join** (LWW dimension). |
| `dag.py` | The immutable logical `StreamGraph` (topological order, cycle detection, describe). |
| `datastream.py` | The fluent `DataStream` / `KeyedStream` / `WindowedStream` builder + `StreamEnvironment` (`from_source` raw values / `from_records` pre-built; `map`/`filter`/`flat_map`/`split`/`key_by`/`union`/`window`/`interval_join`/`join_table`/`connect`). |
| `runtime.py` | `JobExecutor`: deterministic single-threaded engine — watermark **alignment** across inputs, two-input join tagging, checkpoint cadence. |
| `broker.py` | `Broker` protocol boundary to facet A (imported if present, else a minimal `InMemoryBroker`); source/sink connectors. |
| `testkit.py` | `TestHarness` (drive one operator with explicit event time) + `collect`. |
| `pipelines/events.py` | Wire models: `ReaderIntentEvent` (§4.3), `RenderEvent` (§5.6). |
| `pipelines/engagement.py` | Reader-intent analytics: sliding velocity, session shaping (§4.7 idle-pause), activity counts, stall detection. |
| `pipelines/render_qa.py` | Render-event dashboards: throughput, accept/regen rate (§13), CCS, request→clip latency p50/p95. |
| `pipelines/dashboard.py` | Assembles both pipelines into one §13 `MetricsSnapshot` + the §4.6 velocity-adaptive lookahead and the crew-vs-baseline delta. |

## Key design decisions

- **Event time, not processing time.** Every record carries an explicit epoch-ms
  timestamp (the client's `last_activity_ms` / event `ts_ms`); watermarks drive
  all windows and timers. Out-of-order delivery is handled by
  bounded-out-of-orderness watermarks + allowed-lateness; records past the grace
  period go to a side output (counted, never silently dropped).
- **Exactly-once via aligned checkpoints.** The `CheckpointCoordinator`
  snapshots every operator backend under one checkpoint id; recovery restores
  the latest complete checkpoint. Snapshots deep-copy, so a taken checkpoint is
  immutable against later mutation (what a serializing durable backend gives for
  free). Paired with committed broker offsets, replay reproduces identical
  output — verified by `test_determinism_same_input_same_output`.
- **Deterministic runtime.** Single-threaded; inputs to a multi-input node are
  merged by `(timestamp, channel, position)` and watermarks aligned to the input
  channel minimum. Same input ⇒ byte-identical output ⇒ no flaky tests.
- **Logical graph separate from execution.** `StreamGraph` is inspectable /
  serializable (good for a topology view / the `agent_activity` feed); the
  `JobExecutor` instantiates it. One graph, many possible runtimes.
- **Decoupled from facet A.** `broker.py` imports facet A's `Broker` if its
  package exists and otherwise defines a structurally-identical fallback, so this
  facet builds and tests independently while ~29 agents run in parallel.

## Shared-file changes

**None.** This package is entirely new files under `backend/app/streaming/` plus
four new test modules under `backend/tests/`. No existing module, route,
migration, or shared file is modified. No new DB tables (no Alembic revision).

## Tests

Six modules — `test_streaming_processing_core.py`, `…_joins.py`, `…_pipelines.py`,
`…_broker_and_recovery.py`, `…_operators_advanced.py` (union / co-process / split
/ windowed-reduce), `…_dashboard.py` (the §13 snapshot), and
`…_watermarks_and_integration.py` (channel-min watermark alignment, late-data
edges, broker→engine→broker round trip) — 53 deterministic tests, operator-level
(via `TestHarness`) and end-to-end.
Run: `backend/.venv/bin/pytest tests/test_streaming_*.py -q`.
