# Kinora developer docs

Kinora turns a book or PDF into a **page-synced film that generates itself a few
seconds ahead of the reader**. Six AI agents share one versioned "canon" so a
long adaptation stays visually consistent instead of drifting into AI slop.

This portal documents the **public HTTP API** and the two official SDKs you can
build on:

- a typed, isomorphic **TypeScript SDK** (`@kinora/sdk`) — Node 20+ and browsers,
- a typed **Python SDK** (`kinora`) — sync + async clients on `httpx`.

Both are generated from one source-of-truth contract, so the SDKs, the
[API reference](api-reference.html), and the machine-readable
[`openapi.json`](../../clients/spec/openapi.json) never disagree.

## What you can build

The API exposes the whole generation-on-scroll loop:

- **Auth** — register, log in, get a bearer token.
- **Books** — upload a PDF/EPUB, watch ingest progress, read pages, the canon
  graph, and the shot timeline.
- **Sessions** — open a reading session and stream reading-intent updates
  (where the reader is + how fast) so the backend renders the window ahead.
- **Events** — subscribe to a live SSE stream of `clip_ready`, `buffer_state`,
  `agent_activity`, `conflict_choice`, and more.
- **Director tools** — comment on a region to re-render a shot, edit the canon to
  surgically regenerate only the dependent shots, and resolve continuity
  conflicts.
- **Preferences** — read and reset the reader's learned "directing style".
- **Eval / optim** — the watermark buffer trace and cost/perf rollups.

## The two ideas that make it work

> **Consistency is a memory problem, not a model problem.** A persistent,
> versioned story canon conditions every generated clip on the relevant slice of
> truth, so continuity becomes an emergent property of retrieval.

> **The film is a function of attention.** Kinora never renders a whole film — it
> renders the next few seconds, just ahead of your eyes, and caches every
> accepted shot so a re-read costs nothing.

Read the [six-agent architecture](architecture.html) for how the crew maintains
that canon, or jump straight to the [quickstart](getting-started.html).

## A note on the live-video gate

Kinora has a deliberate **`KINORA_LIVE_VIDEO` gate** (off by default). With it
off, the whole loop still runs end-to-end — the render pipeline degrades to a
Ken-Burns pan over a still keyframe and the video budget stays at zero. The API
surface and the SDKs are identical either way; live video just changes what the
render worker produces. None of the examples in these docs require the gate.
