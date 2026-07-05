# KINORA — *watch the book*

> Turn any book or PDF into a **watchable, page-synced film that generates itself a few seconds ahead of wherever you're reading** — produced by a crew of AI agents whose shared memory keeps a feature-length adaptation visually consistent instead of melting into AI slop.

The book stays on screen. As the film plays, a narrator reads the text aloud, the exact words being spoken highlight in sync (karaoke-style), and the page turns itself to follow the playhead. You can watch, read along, or both.

|  |  |
|---|---|
| **Deployment** | Alibaba Cloud — ECS / Function Compute · OSS · DashScope / Model Studio |
| **Status** | **Built and runnable** — full backend + Electron desktop, a native macOS **Liquid Glass** shell, and a browser-served renderer; real Qwen/Wan/Qwen3-TTS, persistence, queue, budget, and recovery workers. Bring up the stack with `docker compose`; deploy with `infra/terraform`. |

> **Run it in 4 commands:** `cp .env.example backend/.env` (add your DashScope key) → `make stack-up` → `make seed-demo` → `make app-desktop-dev`. See [Run it locally](#run-it-locally).

---

## Why this is exciting

Kinora is exciting because it turns a hard product problem into a coherent
system: the book remains readable, the film responds to attention, and the
backend spends only when a scene is likely to matter. The pieces reinforce one
another: canon memory keeps the adaptation consistent, the scheduler keeps motion
ahead of the reader, and the fallback path keeps the experience moving when live
generation is unavailable.

That is the story worth telling: long-form generated video becomes practical
when it is guided by memory, budget, and reader intent.

---

## The two ideas that make it defensible

Most AI-video projects follow the solved demo pattern: *type a prompt → get a 15-second short.* The unsolved problem is **long-form consistency** — across the dozens of clips a long story needs, faces change, palettes drift, and props teleport. Kinora's bet is that this is fixable with architecture, not a bigger model:

- **Consistency is a memory problem, not a model problem.** A persistent, versioned **story canon** — what each character looks like, sounds like, where they are, and what has already happened — conditions every generated clip on the *relevant slice* of that truth. Continuity stops being a dice roll and becomes an emergent property of retrieval.
- **The film is a function of attention.** A 300-page book is ~25 minutes of video and would be insane to pre-render — most of it would never be watched. So Kinora never renders a film. It renders the **next few seconds**, just ahead of your eyes, spending its scarce video budget only on pages a human is actually arriving at, and **caching every accepted shot** so a re-read costs nothing.

These two reframes are what let a single architecture double as the showrunner, the memory system, and the crew that maintains it.

## Why anyone cares

Kinora uses the medium that's *destroying* attention spans — short, autoplaying, scrolling video — to deliver the one thing those attention spans can no longer hold: **books.** It's reading-*adjacent*, not reading-replacing — the words stay front and center, the video pulls you through them. That makes it genuinely useful for:

- **Reluctant readers / ADHD** — the video pulls you forward; synced text keeps you reading words, not just absorbing a cartoon.
- **Dyslexia** — simultaneous audio + highlighted text is an evidence-based decoding aid.
- **Language learners** — watch the scene, hear the line, see the word, at reading pace.
- **Manga / webtoon / indie authors** — instant animated adaptations of static panels.

## How it works

### Generation-on-scroll

A reader *dwells*: a page of ~250 words takes 45–90 seconds to read but maps to only ~8–15 seconds of video. That asymmetry is the whole trick — the backend isn't racing real-time playback, it's racing reading speed, and reading is slow. The forward path is split into three zones:

| Zone | ETA window | What exists | Video budget |
|---|---|---|---|
| **Committed** | 0 – ~45s | Full video, QA-passed, narrated, cached, instantly playable | **spends video-seconds** |
| **Speculative** | ~45 – ~240s | One **keyframe still per beat** (image-gen, not video) | **~zero** |
| **Cold** | > 240s | Plan + canon only (text already analysed at import) | free |

A **dual-watermark buffer with hysteresis** (low = 25s, high = 75s of committed video ahead) makes generation *bursty and event-driven* — it fills to the high mark, then goes completely idle until the buffer drains, so the system is smooth **and** not generating all the time. Speculation is image-only, so guessing ahead is nearly free; video-seconds are spent only when a reader's trajectory confirms they're arriving. Skim too fast, seek, or put the book down, and it degrades gracefully (a Ken-Burns pan over a still keyframe) or quietly waits — never a spinner, never a stall.

### The crew (Agent Society)

Six single-purpose agents, each a separate service with a typed JSON contract, all reading and writing one shared canon through an **MCP server**. No agent holds private mutable state — the canon is the only truth.

| Agent | Job | Model |
|---|---|---|
| **Showrunner** | Plans the production, decomposes the book, **arbitrates conflicts** | Qwen3.7-Max |
| **Adapter** | PDF → screenplay → shot list (with source spans) | Qwen3.5-Plus |
| **Continuity Supervisor** | Owns canon writes; flags inconsistencies; runs forgetting/versioning | Qwen3.7-Plus |
| **Cinematographer** | Designs each shot: keyframe, camera, locked references, Wan mode | Qwen3.5-Plus (VL) |
| **Generator** | Renders the clip + narration | Hosted Wan (`wan2.1-*` demo defaults; `wan2.5/2.2` quality overrides) + Qwen3-TTS |
| **Critic / QA** | Scores each clip against the canon; decides pass / fix / regen | Qwen3-VL |

When the Continuity Supervisor catches a contradiction (e.g. *a shot depicts the heroine drawing a sword she lost three beats ago*), it raises a **structured conflict object** and the Showrunner arbitrates under a fixed policy: evolve the canon if the text supports it, surface to the director if user-facing, otherwise honor the established truth. This negotiation is surfaced live in the app as an inspectable activity feed.

### The memory layer (MemoryAgent)

A versioned **canon graph** (characters, voices, locations, props, style, timeline) plus an **episodic vector store** of every shot ever generated and its QA scores, exposed through a small, deliberate MCP tool surface. It delivers:

- **Recall under a limited context window** — `canon.query(beat)` returns *only* what a beat needs (characters present + active location + style tokens + the previous shot's endpoint frame), never the whole book. Token cost stays flat as books get longer.
- **Timely forgetting** — facts are scoped to the beat interval where they were true; retired states drop out of forward retrieval but survive for backward (time-travel) reads.
- **Increasingly accurate across sessions** — every Director edit writes a preference signal, so the system learns this reader's taste (pacing, palette, framing) and applies it by default next time. The accumulated style is browsable and resettable (per-book or globally) in the **"Your directing style"** Settings panel on both apps (`GET`/`DELETE /me/prefs`, `/books/{id}/prefs`).
- **Free re-reads** — each shot has a content hash; a cache hit serves the clip from OSS for zero video-seconds, which also makes Director edits surgical (only the dependent shots regenerate).

## Architecture

Two planes, deliberately separated. The **control plane** (Scheduler) decides *when and what* to render against the reader's attention; the **creative/data plane** (the crew + memory + infra) decides *how* a scene looks and produces the pixels. The memory store sits at the centre as a shared blackboard, exposed to every agent as an MCP server.

```mermaid
flowchart TB
    subgraph FE["Frontend — two-pane workspace"]
        WS["PDF (left) ⟷ Video (right)"]
        SE["SyncEngine · playhead · focus word w · velocity v"]
    end
    subgraph CTRL["Control plane"]
        SCHED["Scheduler / Prefetch Controller<br/>watermark buffer · promotion · cancel"]
        BUD["Budget service"]
    end
    subgraph CREW["Agent Society — the production crew"]
        SHOW["Showrunner"]
        ADAPT["Adapter"]
        CONT["Continuity Supervisor"]
        CINE["Cinematographer"]
        GEN["Generator · Wan + CosyVoice"]
        CRIT["Critic / QA"]
    end
    subgraph MEM["Memory — MCP canon server"]
        CANON["Canon graph (versioned)"]
        EPI["Episodic / vector store"]
        CACHE["Shot cache (hash-keyed)"]
    end
    subgraph INFRA["Alibaba Cloud"]
        DS["DashScope / Model Studio"]
        OSS["OSS object storage"]
        Q["Render queue + workers"]
    end
    SE -->|"intent / seek"| SCHED
    SCHED <-->|"reserve seconds"| BUD
    SCHED -->|"shot spec request"| CINE
    SCHED -->|"enqueue / cancel"| Q
    SHOW --> ADAPT --> CANON
    SHOW --> CINE --> GEN --> CRIT
    CRIT -->|"pass / fail / fix"| GEN
    CRIT --> EPI
    CONT <--> CANON
    GEN --> CACHE
    GEN --> Q --> DS
    GEN --> OSS
    OSS -->|"clips + sync map"| SE
```

The full diagram, the per-shot state machine, and the end-to-end sequence are in [`kinora.md` §6–§9](./kinora.md#6-system-architecture).

## Tech & model stack

- **Frontend** — two-pane workspace; PDF rendered with PyMuPDF (virtualised pages); a `SyncEngine` that bidirectionally binds scroll ↔ video ↔ word; events over SSE/WebSocket.
- **Models (Qwen Cloud / DashScope)** — Qwen3.7-Max (orchestration), Qwen3.7-Plus / Qwen3.5-Plus (high-volume agents), Qwen3-VL (page reading + QA), hosted Wan (`wan2.1-t2v-turbo` / `wan2.1-i2v-turbo` demo defaults, `wan2.5-t2v-preview` / `wan2.2-i2v-plus` quality overrides), and Qwen3-TTS narration.
- **Backend (Alibaba Cloud)** — API, ingest recovery worker, render worker, MCP, and browser renderer on ECS; clips, frames, audio, and the canon vault in OSS; idempotent Redis/Tair queues and locks.

## Project layout

```
backend/        FastAPI app, six-agent crew, MCP canon-memory server, render pipeline,
                scheduler + Redis queue, budget service, eval harness, Alembic migrations
apps/desktop/   Electron + Vite renderer (React + Tailwind) — the two-pane reading room
apps/desktop-native/ native macOS Liquid Glass shell (showcase; separate from Electron)
infra/          docker-compose.yml (the backend stack) + terraform/ (Alibaba Cloud IaC)
deploy/         alibaba_render_worker.py — the §12.6 OSS + DashScope proof artifact
assets/books/ the bundled public-domain demo book + its PyMuPDF build script
Makefile      install / stack-up / migrate / worker / ingest-worker / mcp / provider-preflight / seed-demo / test / …
kinora.md     the full technical design (architecture, agents, pipeline, memory, budget)
```

## The real process model

Every backend role is the same image with a different command (see `infra/docker-compose.yml`):

| Service | Command | Role |
|---|---|---|
| `api` | `uvicorn app.main:app` | REST + SSE/WS; **runs the Scheduler in-process** + the idle-sweeper; **triggers Phase-A ingest** as a background task on upload |
| `ingest-worker` | `python -m app.ingest.recovery` | Recovers books left `importing` after restarts from the durable `source_pdf_key` |
| `render-worker` | `python -m app.queue.worker` | Drains the Redis priority queue; runs the per-shot pipeline / the ffmpeg degradation ladder |
| `mcp` | `python -m app.mcp.run --http` | The canon-memory MCP server (the §8.3 tool surface) |
| `frontend` | `nginx` over `apps/desktop/dist` | Browser-accessible Vite renderer |
| `migrate` | `alembic -c alembic.ini upgrade head` | One-shot schema apply (runs before the app) |
| `postgres` / `redis` / `minio` | — | Postgres+pgvector · Redis · S3-compatible object storage |

There is **no** separate scheduler process — Scheduler control runs inside `api`.
Upload still starts ingest immediately in `api`, while `ingest-worker` is the durable
restart/recovery loop for interrupted imports.

## Run it locally

**Prerequisites:** Docker + Docker Compose, and a DashScope (Model Studio, intl) API key.

```bash
# 1. Configure secrets (backend/.env is gitignored; .env.example is the template).
cp .env.example backend/.env
#    edit backend/.env: set DASHSCOPE_API_KEY=sk-...   (KINORA_LIVE_VIDEO stays false)
#    and set TTS_MODEL=qwen3-tts-flash  (preset-voice narration; see Configuration)

# 2. Build + bring up the stack (data plane, migrate, api, ingest/render workers, mcp, frontend).
make stack-up                 # == cd infra && docker compose up -d --build
#    migrations run automatically via the one-shot `migrate` service.

# 3. Seed the bundled public-domain demo book through the REAL flow (register → upload → ingest).
make seed-demo                # == python backend/scripts/seed_demo.py --via api

# 4. Run the desktop app (it connects to the API at http://localhost:8000).
make app-install              # pnpm install (first run only)
make app-desktop-dev          # launches the Electron reading room
#    Browser renderer: http://localhost:5173 · API docs: http://localhost:8000/docs · Prometheus: http://localhost:9090
```

### Local dev without Docker (venv)

```bash
make install                  # backend/.venv + pip install -e .[dev]
cd infra && docker compose up -d postgres redis minio minio-bootstrap   # just the data plane
make migrate                  # alembic upgrade head
# then, in separate shells:
cd backend && .venv/bin/uvicorn app.main:app --reload     # api (scheduler + ingest in-process)
make worker                   # python -m app.queue.worker
make ingest-worker            # python -m app.ingest.recovery
make mcp                      # python -m app.mcp.run --http
make seed-demo SEED_ARGS="--via direct"   # or run ingest in-process, no server needed
```

### Run the apps

**Prerequisites:** Node 20+ and `pnpm` (the apps are a pnpm + Turborepo workspace).

```bash
make app-install              # pnpm install (first run)

# Desktop (Electron) — connects to the API at http://localhost:8000:
make app-desktop-dev          # == pnpm --filter @kinora/desktop dev
#   point at another backend with:  VITE_KINORA_API_URL=https://api.example.com
#   package signed installers (needs certs):  pnpm --filter @kinora/desktop dist

# Browser-served renderer image:
docker build -f infra/docker/desktop.Dockerfile \
  --build-arg VITE_KINORA_API_URL=http://localhost:8000 \
  -t kinora-frontend:local .
```

## Verify the end-to-end loop

With `KINORA_LIVE_VIDEO` **off** (the default — no Wan spend), the full loop still runs end to end:

1. **Ingest** — `seed-demo` uploads the demo PDF; Phase A extracts pages + per-word boxes, runs Qwen-VL page analysis, builds the versioned canon (characters/locations/props/style), plans the shot list + source-span index, and identity-locks keyframes + voices. The book reaches `status: ready`.
2. **Session + scroll** — create a reading session and send `intent_update`s; the Scheduler fills the committed buffer under the dual-watermark and enqueues **keyframe** work across the speculative horizon (zero video-seconds).
3. **Render** — the `render-worker` drains the queue. With the live gate off it steps down the **degradation ladder** and produces a **real Ken-Burns mp4** over the locked keyframe (muxed with CosyVoice narration), surfaced as a `clip_ready` event — **zero video-seconds spent**. The budget ledger stays at 0.
4. **Go live** — run `make provider-preflight` first, flip `KINORA_LIVE_VIDEO=1`, and the same committed lane renders **real hosted Wan video** through the Critic/cache/budget path, persists the downloaded clip to OSS/MinIO, and hot-swaps it into the workspace; the budget service decrements and enforces the hard ceiling.

This loop is exercised by the backend test suite (`make test`, against throwaway Postgres+Redis+MinIO) and by `make seed-demo`.

## Configuration & the go-live gate

All config flows through typed settings (`backend/app/core/config.py`); see [`.env.example`](./.env.example) for every key. The ones that matter most:

| Setting | Default | Meaning |
|---|---|---|
| `DASHSCOPE_API_KEY` | — (**required**) | Model Studio (intl) key. Only in gitignored `backend/.env`. |
| `KINORA_LIVE_VIDEO` | `false` | **Go-live gate (§11.1).** Off = the pipeline degrades to Ken-Burns (zero Wan spend) while you iterate. On = real Wan video renders. |
| `VIDEO_MODEL` / `_I2V` / `_R2V` | `wan2.1-t2v-turbo` / `wan2.1-i2v-turbo` | Hosted Wan model ids. Quality overrides: `wan2.5-t2v-preview`, `wan2.2-i2v-plus`. Avoid `wan2.2-t2v-plus`. |
| `TTS_MODEL` | `qwen3-tts-flash` | TTS model. `qwen3-tts-flash` serves the preset voices ingest assigns (Cherry, Ryan, …); `qwen3-tts-vc` is the voice-clone model and expects an enrolled voice. |
| `BUDGET_CEILING_VIDEO_S` | `1650` | Hard cap on total video-seconds. Per-session/per-scene sub-caps also apply. |
| `WATERMARK_LOW_S` / `_HIGH_S` / `COMMIT_HORIZON_S` | `25 / 75 / 45` | Scheduler buffer + promotion horizons. |

The budget service enforces the ceiling with a real append-only ledger and a transaction-scoped lock; the gate prevents silent credit burn. Real Wan renders spend real, metered DashScope credits — flip the gate on deliberately.

**Auth model — local vs cloud.** The API/MCP enforce three env values: `JWT_SECRET` (the app refuses to boot in non-local on the insecure built-in default), `MCP_AUTH_TOKEN` (the bearer the MCP server requires), and `CORS_ORIGINS` (the allowed browser origin[s]; credentialed CORS, so **no wildcard**). Locally these are pre-wired with **dev** values in `infra/docker-compose.yml` (and `APP_ENV` stays `local`, so the JWT default is tolerated), so `make stack-up` just works. In **cloud** they're real secrets provisioned + injected by Terraform/cloud-init — `jwt_secret`/`mcp_auth_token` auto-generate and `cors_origins` is required (see [Deploy to Alibaba Cloud](#deploy-to-alibaba-cloud)).

## Deploy to Alibaba Cloud

`infra/terraform/` is ready-to-apply IaC (validated with `terraform validate` + `terraform fmt`; **not** applied — it needs your credentials). It provisions VPC + security groups, **OSS** (object storage), **ApsaraDB RDS for PostgreSQL** (pgvector), **Tair/Redis**, and **ECS** nodes for `frontend`, `api`, `ingest-worker`, `render-worker`, and `mcp`.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # add Alibaba creds + DashScope key (gitignored)
terraform init && terraform validate && terraform plan && terraform apply
```

**Production security model (set before `apply` — it fails closed):**

| Input | Required? | What it does |
|---|---|---|
| `admin_cidr` | **yes** (no default; rejects `0.0.0.0/0`) | CIDR allowed to reach the **API (8000)** — your frontend/LB or office egress |
| `ssh_cidr` | **yes** (no default; rejects `0.0.0.0/0`) | CIDR allowed to **SSH (22)** — ideally a bastion/VPN `/32`, kept separate from app access |
| `cors_origins` | **yes** (no default; no `*`) | The deployed **frontend origin(s)**, injected as `CORS_ORIGINS` (credentialed CORS can't use a wildcard) |
| `jwt_secret` | auto-generates if empty | Injected as `JWT_SECRET` so prod never boots on the insecure built-in default |
| `mcp_auth_token` | auto-generates if empty | Injected as `MCP_AUTH_TOKEN`, the bearer the MCP server requires |

The **MCP port (8765) is intra-VPC only** (never internet-facing); the bearer token is defense-in-depth on top. cloud-init writes these into each node's env **without** shell tracing, so secrets never land in `cloud-init-output.log`. Read back the generated secrets with `terraform output -raw jwt_secret` / `-raw mcp_auth_token`. For real prod, prefer KMS / Secrets Manager / OOS over user_data.

The **Electron app** ([`apps/desktop`](./apps/desktop)) is the primary local product. For browser deployment, build the renderer image from [`infra/docker/desktop.Dockerfile`](./infra/docker/desktop.Dockerfile) with `VITE_KINORA_API_URL` pointed at the deployed API and push it to `frontend_container_image`.

The **proof-of-deployment artifact** ([`deploy/alibaba_render_worker.py`](./deploy/alibaba_render_worker.py), kinora.md §12.6) is a real render worker that demonstrably uses **OSS** + **DashScope** — it reuses the app's `ObjectStore`, `VideoProvider`, and queue worker rather than duplicating logic. See [`deploy/README.md`](./deploy/README.md) and [`infra/terraform/README.md`](./infra/terraform/README.md).

## Repository contents

| Path | What it is |
|---|---|
| [`backend/`](./backend) · [`apps/desktop`](./apps/desktop) · [`apps/desktop-native`](./apps/desktop-native) | The built application (FastAPI backend · Electron/Vite renderer · native macOS showcase). |
| [`infra/`](./infra) · [`deploy/`](./deploy) · [`assets/`](./assets) | Local stack + Alibaba IaC · §12.6 proof artifact · demo book. |
| [`kinora.md`](./kinora.md) | The full technical design — architecture, agents, pipeline, memory, budget. |
| [`what-is-kinora.md`](./what-is-kinora.md) | Plain-English explainer. **Start here if you're non-technical.** |

## License

[Apache-2.0](./LICENSE).
