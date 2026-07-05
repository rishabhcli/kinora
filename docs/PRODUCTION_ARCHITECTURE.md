# KINORA — Production Architecture & Multi-Track Strategy

> How Kinora covers Tracks 1, 2, 3, and 4 with one coherent architecture — and what makes it a real product, not a demo.

---

## Track Coverage Map

| Track | Name | Kinora's Role | Depth |
|---|---|---|---|
| **Track 2** | AI Showrunner | **Primary** — the entire video generation pipeline | Full |
| **Track 1** | MemoryAgent | **Core** — versioned canon graph, episodic store, forgetting, preference learning | Full |
| **Track 3** | Agent Society | **Core** — 6-agent crew + Production Autopilot, negotiation protocol, conflict resolution | Full |
| **Track 4** | Autopilot Agent | **Integrated** — automated production workflow with HITL checkpoints, error remediation, quality gates | Full |

**The key insight:** Track 4 isn't a separate feature — it's a **layer** that wraps the entire pipeline. The creative pipeline (Tracks 1-3) produces video; the Autopilot layer (Track 4) manages the pipeline as a production-grade business workflow with automated decision-making, error recovery, and human-in-the-loop checkpoints at critical junctures.

---

## What Makes Kinora Production-Grade (Not a Toy)

### 1. Real Error Recovery, Not Just Try/Catch

Every failure mode has a **specific, automated recovery strategy** — not a generic "retry 3 times and give up":

| Failure | Toy Response | Production Response |
|---|---|---|
| DashScope API timeout | Retry, then fail | Switch to cheaper model (`qwen3.5-flash` for routing), retry with exponential backoff, if persistent → switch to batch API, if still failing → queue for offline retry + serve from degradation ladder |
| Video generation rejected (content policy) | Fail silently | Autopilot rewrites prompt to remove flagged content, retries with sanitized prompt, logs the rejection pattern to episodic memory so future prompts avoid the same trigger |
| CCS threshold miss (face drift) | Regenerate blindly | Critic diagnoses *which* reference is drifting → Cinematographer re-locks reference with tighter crop → regenerate with `first_and_last_frame` mode instead of `reference_to_video` → if still failing, escalate to Director |
| Budget exhausted mid-session | Hard stop | Autopilot computes remaining budget → prioritizes the most impactful shots (climax scenes over transitions) → degrades non-critical shots to Ken-Burns → notifies Director with cost-benefit analysis |
| OSS upload fails | Lose the clip | Retry with multipart upload → if persistent, write to local disk + queue for OSS sync → clip is still served from local cache |
| Canon corruption (bad write) | Crash | Versioned canon means rollback to last good version → log the corruption → rebuild affected indices → continue operation |

### 2. The Production Autopilot Agent (Track 4)

A seventh agent — the **Production Manager** — that doesn't touch creative decisions but runs the **business workflow** of the pipeline:

```
┌─────────────────────────────────────────────────────────────┐
│              PRODUCTION AUTOPILOT LAYER                      │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Intake &     │  │ Quality      │  │ Budget       │      │
│  │ Triage       │  │ Gates        │  │ Optimizer    │      │
│  │              │  │              │  │              │      │
│  │ • Classify   │  │ • Stage      │  │ • Real-time  │      │
│  │   PDF        │  │   checks     │  │   spend      │      │
│  │ • Safety     │  │ • Auto-      │  │ • Reallocate │      │
│  │   scan       │  │   proceed    │  │   between    │      │
│  │ • Cost       │  │ • Escalate   │  │   scenes     │      │
│  │   estimate   │  │ • HITL gate  │  │ • Impact     │      │
│  │ • Route to   │  │              │  │   ranking    │      │
│  │   pipeline   │  │              │  │              │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Remediation  │  │ HITL         │  │ Reporting    │      │
│  │ Engine       │  │ Orchestrator │  │ & Audit      │      │
│  │              │  │              │  │              │      │
│  │ • Auto-      │  │ • Meaningful │  │ • Cost       │      │
│  │   recover    │  │   checkpoints│  │   breakdown  │      │
│  │ • Model      │  │ • Context    │  │ • Quality    │      │
│  │   fallback   │  │   rich       │  │   metrics    │      │
│  │ • Prompt     │  │ • Async      │  │ • Production │      │
│  │   repair     │  │   resume     │  │   report     │      │
│  │ • Degrade    │  │              │  │ • Audit trail│      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

#### Intake & Triage

When a PDF is uploaded, the Production Manager runs an automated intake workflow:

1. **Classify** — `qwen3.6-flash` classifies the document: genre, estimated length, character count, complexity score, visual richness
2. **Safety scan** — Check for content that might trigger DashScope content policy (violence, adult content). Pre-flag risky passages for the Adapter to handle carefully
3. **Cost estimate** — Based on page count, character count, and complexity, estimate total video-seconds and token cost. Show the user: "This book will take approximately 340 video-seconds and 2.1M tokens to fully adapt"
4. **Route** — Decide: proceed automatically (simple, safe content) or flag for human review (complex, potentially risky content)
5. **Budget allocation** — Allocate the global budget across scenes based on narrative impact (climax scenes get more video-seconds than transitions)

#### Quality Gates

At each pipeline stage, the Production Manager runs an automated quality gate:

| Stage | Gate Check | Auto-Proceed If | Escalate If |
|---|---|---|---|
| Ingest | Canon completeness | All characters have reference images + cloned voices | Missing references for main characters |
| Adaptation | Shot list coverage | All beats have shots with source spans | Gaps in source-span index |
| Keyframe | Style consistency | Style embeddings within centroid threshold | Style drift detected |
| Video render | CCS + style + timeline + motion | All 4 checks pass (existing Critic loop) | Any check fails after retry cap |
| Stitch | Audio sync | Word timestamps within 50ms of audio | Timestamp drift detected |
| Final | Overall production quality | QA score > threshold | Below threshold → Director review |

Each gate either auto-proceeds, auto-remediates, or escalates to HITL. The gate decisions are **logged with reasoning** so the automated decision-making is inspectable.

#### Budget Optimizer

Not just tracking spend — **actively optimizing** where video-seconds go:

- **Impact ranking** — Score each planned shot by narrative importance (climax > confrontation > dialogue > transition > establishing). Spend video-seconds on high-impact shots first.
- **Real-time reallocation** — If a scene comes in under budget (fewer regens than expected), reallocate surplus to the next high-impact scene
- **Cost-benefit for Director edits** — When a Director requests a change, compute: "This edit costs 15 video-seconds and affects 3 shots. Remaining budget: 847s. Proceed?" — making the tradeoff explicit
- **Degradation strategy** — When budget is low, degrade low-impact shots to Ken-Burns first, preserve video quality for high-impact scenes

#### Remediation Engine

The automated error recovery system. When something fails, the Remediation Engine:

1. **Diagnoses** the failure type (API error, content rejection, quality miss, timeout)
2. **Selects** a recovery strategy from a strategy table (not random retry)
3. **Executes** the recovery (switch model, repair prompt, change Wan mode, degrade)
4. **Logs** the failure + recovery to episodic memory so the system learns what works
5. **Escalates** to HITL only if all automated strategies are exhausted

```python
# Simplified remediation strategy table
REMEDIATION_STRATEGIES = {
    "api_timeout": [
        ("retry_backoff", {"retries": 3, "backoff": [2, 8, 30]}),
        ("switch_model", {"from": "qwen3.6-plus", "to": "qwen3.5-plus"}),
        ("switch_to_batch_api", {}),
        ("degrade", {"level": "keyframe_kenburns"}),
    ],
    "content_rejected": [
        ("sanitize_prompt", {"remove_flags": ["violence", "adult"]}),
        ("rewrite_scene", {"tone": "softer"}),
        ("degrade", {"level": "narration_only"}),
        ("escalate_hitl", {"reason": "content_policy"}),
    ],
    "ccs_fail": [
        ("tighten_references", {"crop": "face_only"}),
        ("switch_wan_mode", {"to": "first_and_last_frame"}),
        ("relock_character", {"regenerate_ref": True}),
        ("degrade", {"level": "keyframe_kenburns"}),
    ],
    "budget_exhausted": [
        ("reallocate", {"from_low_impact": True}),
        ("degrade_low_impact", {"level": "kenburns"}),
        ("degrade_all", {"level": "narration_only"}),
        ("escalate_hitl", {"reason": "budget_override"}),
    ],
}
```

#### HITL Orchestrator

Human-in-the-loop checkpoints that are **meaningful, not ceremonial**:

- **Context-rich escalation** — When escalating to a human, provide: what happened, what was tried, what the options are, and the cost of each option. Not "something failed, click OK."
- **Async resume** — The pipeline doesn't block while waiting for human input. It continues working on other shots and resumes the blocked shot when the human responds.
- **Decision logging** — Every HITL decision is logged with the context and the human's choice, so the system learns from human decisions over time (preference learning).
- **Batch review** — Instead of interrupting for every single decision, batch low-urgency decisions into a periodic review queue. Only interrupt for urgent, blocking decisions.

**HITL checkpoint types:**

| Checkpoint | When | What the human decides |
|---|---|---|
| Content safety review | Intake flags risky content | Proceed, skip scene, or modify |
| Creative direction | Showrunner detects ambiguous adaptation | Choose between 2-3 creative options |
| Budget override | Budget optimizer requests more than allocated | Approve extra spend or accept degradation |
| Final quality | Production quality below threshold | Accept, request changes, or regenerate |
| Conflict resolution | Canon conflict with no clear policy answer | Choose: honor canon, evolve, or ask user |

#### Reporting & Audit

Automated production reports generated for every session:

- **Cost breakdown** — Video-seconds spent per scene, per shot, per agent call. Token usage per model.
- **Quality metrics** — CCS per character, style drift per scene, regeneration rate, accepted-footage efficiency.
- **Production timeline** — Wall-clock time per stage, bottlenecks identified.
- **Audit trail** — Every automated decision, every HITL interaction, every error and recovery — logged and inspectable.
- **Comparison report** — Crew + memory + autopilot vs. single-agent baseline (the eval harness from the design).

---

## The Seven-Agent Architecture (Updated)

| Agent | Job | Model | Track |
|---|---|---|---|
| **Showrunner** | Plans production, decomposes book, arbitrates creative conflicts | `qwen3.6-plus` | T2, T3 |
| **Adapter** | PDF → screenplay → shot list | `qwen3.5-plus` | T2 |
| **Continuity Supervisor** | Owns canon writes, flags inconsistencies, versioning/forgetting | `qwen3.6-plus` | T1, T3 |
| **Cinematographer** | Shot design: keyframe, camera, refs, Wan mode | `qwen3.6-plus` (vision built-in) | T2 |
| **Generator** | Renders clip + narration | `wan2.7-i2v` / `happyhorse-1.0-t2v` + `cosyvoice-v3-plus` | T2 |
| **Critic / QA** | Scores clips against canon, decides pass/fix/regen | `qwen3.6-plus` (vision built-in) | T2, T3 |
| **Production Manager** | **Autopilot: intake, quality gates, budget optimization, remediation, HITL, reporting** | `qwen3.6-flash` + `qwen3.6-plus` (for complex decisions) | **T4** |

The Production Manager is deliberately lightweight — it uses `qwen3.6-flash` for most routing/classification decisions (cheap, fast) and only escalates to `qwen3.6-plus` for complex judgment calls (budget reallocation strategy, content safety decisions).

---

## What Makes This Unique (That Other Teams Won't Have)

### 1. Generation-on-Scroll (No One Else Will Build This)

Every other Track 2 team will build "prompt → 15-second video." Kinora generates video **as a function of human attention** — just ahead of where you're reading, spending budget only on what will actually be watched. This is a fundamentally different product, not a better version of the same thing.

### 2. Consistency-as-Memory (The Canon Graph)

Other teams will either accept AI slop or throw a bigger model at it. Kinora's bet: consistency is a **retrieval** problem. The versioned canon graph conditions every clip on the relevant slice of truth. This is the Track 1 contribution that makes the Track 2 output actually good.

### 3. The Autopilot Layer (Track 4 Integration)

Other teams' pipelines are either fully automated (and break silently) or fully manual (and slow). Kinora's Production Manager adds **meaningful automation with intelligent escalation** — the pipeline runs itself when it can, escalates to humans when it should, and logs everything for audit.

### 4. Self-Improving Across Sessions

The episodic store + preference learning means the system **gets better with use**. Every accepted shot, every Director edit, every HITL decision feeds back into memory. The second book adapted is faster, cheaper, and higher quality than the first. No other team will have this.

### 5. The Negotiation Protocol (Track 3)

Structured conflict objects, the Showrunner arbitration policy, and the live agent activity feed make multi-agent collaboration **visible and measurable**. Other teams will claim "we use multiple agents" but can't show them negotiating. Kinora can.

### 6. Real Budget Optimization (Not Just Tracking)

The budget optimizer doesn't just count video-seconds — it **ranks shots by narrative impact**, reallocates surplus in real-time, and makes cost-benefit tradeoffs explicit to the Director. This is production-grade resource management, not a counter.

### 7. Multi-Track Coherence

One architecture, four tracks. Not four bolted-together features — the tracks are **emergent properties of the same design**:
- Memory (T1) → enables consistency → enables quality video (T2)
- Multiple agents (T3) → maintain the memory → produce the video
- Autopilot (T4) → manages the pipeline → makes it production-ready

The tracks aren't forced — they're the natural decomposition of a real product.

---

## Production-Grade Features (Beyond the Design Doc)

### Multi-Session Concurrency

The design mentions per-session fairness but doesn't detail it. For production:

- **Session isolation** — Each reading session has its own Scheduler state, buffer, and budget allocation. Sessions don't interfere.
- **Shared cache** — The shot cache is shared across sessions. If two users read the same book, the second gets cache hits for free.
- **Concurrent render management** — Global render pool with per-session quotas. No single user can starve the system.

### Content Safety Pipeline

- **Pre-scan** — During intake, `qwen3.6-flash` scans the PDF for content that might trigger DashScope's content policy
- **Prompt sanitization** — The Cinematographer's prompts are automatically sanitized to remove flagged terms before sending to Wan
- **Rejection learning** — When DashScope rejects a prompt, the rejection pattern is logged. Future prompts for similar scenes avoid the same triggers.
- **HITL for borderline** — Genuinely ambiguous content (e.g., a battle scene in a historical text) is escalated to a human, not auto-rejected

### Observability Dashboard

Not just for the demo — a real monitoring surface:

- **Real-time** — Buffer occupancy, render queue depth, active sessions, budget burn rate
- **Historical** — CCS trends over time, regeneration rate by scene, cost per accepted minute
- **Alerting** — Budget below threshold, error rate spike, DashScope latency anomaly
- **Audit** — Every automated decision, every HITL interaction, every error recovery — searchable log

### API Design (For Real Use)

The backend exposes a clean REST + WebSocket API, not just a UI:

```
POST   /api/books/upload              # Upload PDF
GET    /api/books                     # List books
GET    /api/books/{id}                # Book metadata + ingest status
POST   /api/sessions                  # Start reading session
PUT    /api/sessions/{id}/intent      # Update focus word + velocity
POST   /api/sessions/{id}/seek        # Seek to position
POST   /api/sessions/{id}/comment     # Director comment
PUT    /api/sessions/{id}/canon       # Canon edit
GET    /api/sessions/{id}/metrics     # Real-time metrics
WS     /api/sessions/{id}/events      # Real-time event stream
GET    /api/books/{id}/canon          # Inspect canon graph
GET    /api/books/{id}/shots          # List all shots + QA
GET    /api/production/report         # Production report
```

This means Kinora isn't just a web app — it's a **platform**. Other applications could use the API to build their own interfaces on top of the same pipeline.

### Persistence & Recovery

- **Canon graph** — SQLite (MVP) / PostgreSQL (production). Survives restarts.
- **Episodic store** — FAISS index persisted to disk. Rebuilt from SQLite if corrupted.
- **Shot cache** — OSS (permanent). Content-hash keyed. Never expires.
- **Session state** — Redis (if available) or in-memory with periodic SQLite snapshots.
- **Render queue** — MNS (persistent). Jobs survive worker restarts.

If the server crashes mid-session, the user can reconnect and the system resumes: cached shots are served from OSS, in-flight renders continue from the queue, the buffer refills from the new position.

---

## Updated Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        FRONTEND (React)                           │
│  ┌──────────────┐  ┌────────────────────────────────────────┐    │
│  │  PDF Reader   │  │  Video Stage (Viewer/Director)         │    │
│  │  (PyMuPDF)    │  │  SyncEngine · playhead · w · v         │    │
│  └──────────────┘  └────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Production Dashboard: metrics, agent feed, HITL queue    │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
          │                                    │
          ▼                                    ▲
┌──────────────────────────────────────────────────────────────────┐
│                   CONTROL PLANE                                   │
│  ┌────────────────┐  ┌──────────┐  ┌────────────────────────┐   │
│  │ Scheduler       │  │ Budget   │  │ PRODUCTION AUTOPILOT   │   │
│  │ Watermark buf   │←→│ Service  │  │ (Track 4)              │   │
│  │ Promotion       │  │          │  │                        │   │
│  │ Cancel          │  │          │  │ • Intake & Triage      │   │
│  └────────────────┘  └──────────┘  │ • Quality Gates        │   │
│                                    │ • Budget Optimizer     │   │
│                                    │ • Remediation Engine   │   │
│                                    │ • HITL Orchestrator    │   │
│                                    │ • Reporting & Audit    │   │
│                                    └────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
          │                                    │
          ▼                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│              CREATIVE PLANE — AGENT SOCIETY (Track 3)             │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐               │
│  │Showrunner│→ │ Adapter  │→ │Continuity Sup.   │               │
│  │qwen3.6- │  │qwen3.5-  │  │qwen3.6-         │               │
│  │plus     │  │plus      │  │plus              │               │
│  └──────────┘  │plus      │  └──────────────────┘               │
│                └──────────┘                                      │
│  ┌──────────────────┐  ┌──────────┐  ┌──────────┐              │
│  │ Cinematographer  │→ │ Generator│→ │ Critic/QA│              │
│  │ qwen3.6-plus    │  │wan2.7+   │  │qwen3.6-  │              │
│  │ (vision)        │  │cosyvoice │  │plus(vis) │              │
│  └──────────────────┘  └──────────┘  └──────────┘              │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Production Manager (qwen3.6-flash + qwen3.6-plus)    │   │
│  │  Monitors all agents, runs quality gates, manages HITL   │   │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
          │ all agents read/write through:
          ▼
┌──────────────────────────────────────────────────────────────────┐
│              MEMORY LAYER — MCP CANON SERVER (Track 1)            │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │ Canon Graph │  │ Episodic/    │  │ Shot Cache   │            │
│  │ (versioned) │  │ Vector Store │  │ (hash-keyed) │            │
│  │ + Forgetting│  │ + Recall     │  │ + Dedup      │            │
│  └─────────────┘  └──────────────┘  └──────────────┘            │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Preference Store (cross-session learning)                │   │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│              ALIBABA CLOUD INFRASTRUCTURE                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │ DashScope/   │  │ OSS Storage  │  │ Render Queue │           │
│  │ Model Studio │  │ clips/frames │  │ (MNS)        │           │
│  │ + Batch API  │  │ + canon vault│  │ + DLQ        │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │ ECS / FC     │  │ OpenSearch/  │  │ Redis        │           │
│  │ (agents)     │  │ FAISS        │  │ (sessions)   │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
└──────────────────────────────────────────────────────────────────┘
```

---

## How Each Track Is Demonstrated in the Demo

| Demo Segment | Track | What to Show |
|---|---|---|
| 0:00–0:25 — Viewer mode playing | T2 | Video generating as the reader reads, karaoke highlight, auto page-turn |
| 0:25–1:10 — Generation-on-scroll | T2 | Buffer hairline filling in bursts, Ken-Burns bridge on fast scroll |
| 1:10–1:50 — Director mode edit | T1, T2 | Region-select → "make coat crimson" → canon updates → only dependent shots regenerate → **cost displayed: "3 shots, 15 video-seconds"** |
| 1:50–2:15 — Agent negotiation | T3 | Live agent feed: continuity conflict (lost sword) → Showrunner arbitration → resolution. Agents visibly negotiating. |
| 2:15–2:40 — Autopilot in action | T4 | Production Manager: automated quality gate catches a bad shot → remediation engine auto-recovers (switches Wan mode) → HITL checkpoint for budget override → automated decision log is visible |
| 2:40–3:00 — Metrics + vision | T1, T3 | CCS chart (crew vs. baseline), buffer sawtooth, production report. "Any book, any reader, any attention span." |

---

## Production-Grade Checklist (What Makes It Real)

- [ ] **Real Alibaba Cloud deployment** — ECS + OSS + MNS + DashScope, not localhost
- [ ] **Error recovery** — Every failure mode has a specific automated recovery strategy
- [ ] **Budget optimization** — Impact-ranked spending, real-time reallocation, cost-benefit for edits
- [ ] **HITL checkpoints** — Meaningful, context-rich, async, logged
- [ ] **Content safety** — Pre-scan, prompt sanitization, rejection learning
- [ ] **Multi-session** — Concurrent users with isolated state, shared cache
- [ ] **Persistence** — Data survives restarts (SQLite/PostgreSQL + OSS + FAISS on disk)
- [ ] **Observability** — Real-time dashboard, historical metrics, audit trail
- [ ] **API** — Clean REST + WebSocket API, not just a UI (platform, not app)
- [ ] **Audit trail** — Every automated decision logged with reasoning
- [ ] **Self-improving** — Episodic memory + preference learning across sessions
- [ ] **Open-source** — MIT license, clean repo, documentation
