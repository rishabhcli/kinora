# KINORA — Project Overview

> **One sentence:** Kinora turns any book or PDF into a watchable, page-synced film that generates itself a few seconds ahead of wherever you're reading — produced by a crew of AI agents whose shared memory keeps a feature-length adaptation visually consistent, managed by a production autopilot that handles errors, budgets, and quality gates autonomously with human-in-the-loop checkpoints at critical decisions.

---

## Quick Facts

| | |
|---|---|
| **Project name** | Kinora (working name — alternatives: Reverie, Flipreel, Vellum) |
| **Hackathon** | Global AI Hackathon Series with Qwen Cloud |
| **Primary track** | Track 2 — AI Showrunner |
| **Secondary coverage** | Track 1 (MemoryAgent) + Track 3 (Agent Society) + Track 4 (Autopilot Agent) |
| **Submission deadline** | Jul 9, 2026 · 2:00pm PDT |
| **Judging period** | Jul 10 – Jul 31, 2026 |
| **Winners announced** | On or around Aug 7, 2026 |
| **Prize** | $7,000 cash + $3,000 cloud credits (per track) + blog feature + swag |
| **Deployment target** | Alibaba Cloud (ECS / Function Compute · OSS · DashScope / Model Studio) |
| **Free-tier ceiling** | ~1,650 video-seconds + ~70M tokens, 90 days, Singapore endpoint |
| **API base URL** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| **API credits** | $40 Qwen Cloud voucher (claim via coupon form) |
| **Status** | Design complete · implementation in progress |

---

## The Two Core Ideas

### Thesis A — Consistency is a memory problem, not a model problem

A persistent, versioned **story canon** — what each character looks like, sounds like, where they are, and what has already happened — conditions every generated clip on the *relevant slice* of that truth. Continuity stops being a dice roll and becomes an emergent property of retrieval.

### Thesis B — The film is a function of attention

A 300-page book is ~25 minutes of video and would be insane to pre-render — most of it would never be watched. Kinora never renders a film. It renders the **next few seconds**, just ahead of your eyes, spending its scarce video budget only on pages a human is actually arriving at, and **caching every accepted shot** so a re-read costs nothing.

### Thesis C — The pipeline is a production workflow, not a demo

The entire adaptation pipeline — from PDF intake to finished video — is managed by a **Production Autopilot** (Track 4) that runs automated quality gates, handles error remediation, optimizes budget allocation in real-time, and escalates to human-in-the-loop checkpoints only when judgment is genuinely needed. This isn't a "click generate and pray" tool — it's a managed production pipeline that runs itself when it can and asks for help when it should.

These three reframes let a single architecture win Track 2 (the showrunner), satisfy Track 1 (the memory), require Track 3 (the crew that maintains it), and demonstrate Track 4 (the autopilot that manages it all).

---

## Why Anyone Cares

Kinora uses the medium that's *destroying* attention spans — short, autoplaying, scrolling video — to deliver the one thing those attention spans can no longer hold: **books.** It's reading-*adjacent*, not reading-replacing — the words stay front and center, the video pulls you through them.

**Target users:**
- **Reluctant readers / ADHD** — the video pulls you forward; synced text keeps you reading words, not just absorbing a cartoon
- **Dyslexia** — simultaneous audio + highlighted text is an evidence-based decoding aid
- **Language learners** — watch the scene, hear the line, see the word, at reading pace
- **Manga / webtoon / indie authors** — instant animated adaptations of static panels

---

## How It Works (High Level)

### Generation-on-scroll

A reader *dwells*: a page of ~250 words takes 45–90 seconds to read but maps to only ~8–15 seconds of video. That asymmetry is the whole trick — the backend isn't racing real-time playback, it's racing reading speed, and reading is slow.

| Zone | ETA window | What exists | Video budget |
|---|---|---|---|
| **Committed** | 0 – ~45s | Full video, QA-passed, narrated, cached, instantly playable | **Spends video-seconds** |
| **Speculative** | ~45 – ~240s | One keyframe still per beat (image-gen, not video) | **~zero** |
| **Cold** | > 240s | Plan + canon only (text already analysed at import) | Free |

A **dual-watermark buffer with hysteresis** (low = 25s, high = 75s of committed video ahead) makes generation *bursty and event-driven* — it fills to the high mark, then goes completely idle until the buffer drains.

### The crew (Agent Society + Production Autopilot)

Seven agents — six creative + one production manager — each a separate service with a typed JSON contract, all reading and writing one shared canon through an **MCP server**.

| Agent | Job | Model | Track |
|---|---|---|---|
| **Showrunner** | Plans the production, decomposes the book, arbitrates conflicts | `qwen3.6-plus` | T2, T3 |
| **Adapter** | PDF → screenplay → shot list (with source spans) | `qwen3.5-plus` | T2 |
| **Continuity Supervisor** | Owns canon writes; flags inconsistencies; runs forgetting/versioning | `qwen3.6-plus` | T1, T3 |
| **Cinematographer** | Designs each shot: keyframe, camera, locked references, Wan mode | `qwen3.6-plus` (vision built-in) | T2 |
| **Generator** | Renders the clip + narration | `wan2.7-i2v` / `happyhorse-1.0-t2v` + `cosyvoice-v3-plus` | T2 |
| **Critic / QA** | Scores each clip against the canon; decides pass / fix / regen | `qwen3.6-plus` (vision built-in) | T2, T3 |
| **Production Manager** | **Autopilot: intake, quality gates, budget optimization, remediation, HITL, reporting** | `qwen3.6-flash` + `qwen3.6-plus` | **T4** |

### The memory layer (MemoryAgent)

A versioned **canon graph** (characters, voices, locations, props, style, timeline) plus an **episodic vector store** of every shot ever generated and its QA scores, exposed through a small, deliberate MCP tool surface.

- **Recall under a limited context window** — `canon.query(beat)` returns *only* what a beat needs
- **Timely forgetting** — facts are scoped to the beat interval where they were true
- **Increasingly accurate across sessions** — every Director edit writes a preference signal
- **Free re-reads** — each shot has a content hash; a cache hit serves the clip from OSS for zero video-seconds

---

## Architecture (Three Planes)

- **Control plane** (Scheduler + Production Autopilot) — decides *when and what* to render, manages quality gates, budget, error recovery, and HITL
- **Creative/data plane** (the crew + memory + infra) — decides *how* a scene looks and produces the pixels
- **Memory store** — sits at the centre as a shared blackboard, exposed to every agent as an MCP server

See [`PRODUCTION_ARCHITECTURE.md`](./PRODUCTION_ARCHITECTURE.md) for the full multi-track architecture, including the Production Autopilot layer (Track 4).

---

## Judging Criteria & How We Win

| Criterion | Weight | How Kinora wins |
|---|---|---|
| **Innovation & AI Creativity** | 30% | Three novel moves: film-as-function-of-attention, consistency-as-retrieval, pipeline-as-production-workflow. MCP server shared by all agents. 7-agent architecture covering 4 tracks. |
| **Technical Depth & Engineering** | 30% | Custom MCP server, multi-model orchestration, closed-loop VL critic, watermark-buffered scheduler, budget optimizer, remediation engine, HITL orchestrator, cancellable render queue, content safety pipeline. |
| **Problem Value & Impact** | 25% | Anti-brainrot literacy + accessibility (ADHD, dyslexia, ESL) + manga/indie-author adaptation. Production-grade pipeline, not a demo. Real API, real deployment, real error recovery. |
| **Presentation & Documentation** | 15% | Architecture diagram, live agent-activity feed, buffer-occupancy sawtooth, metrics panel, production dashboard with audit trail, HITL checkpoint demo. |

---

## Repository Structure (Proposed)

```
QwenCloudHackathon/
├── README.md                          # Project front door
├── PROJECT_OVERVIEW.md                # This file — executive summary
├── TECHNICAL_SPEC.md                  # Full technical architecture
├── PRODUCTION_ARCHITECTURE.md          # Multi-track strategy & production autopilot
├── TECH_STACK.md                      # Framework & language recommendations
├── HACKATHON_REQUIREMENTS.md          # Submission checklist & rules
├── IMPROVEMENTS_AND_SUGGESTIONS.md    # Gaps, risks, improvements
├── BUILD_ROADMAP.md                   # 18-day build plan
├── LICENSE                            # Open-source license (MIT or Apache-2.0)
├── Information/
│   ├── mdFile/
│   │   └── kinora.md                  # Original full technical design
│   └── pdfFile/
│       ├── kinora.md.pdf              # PDF version of design
│       └── Qwen Cloud AI Showrunner...pdf  # Original project plan from teammate
├── frontend/                          # React + Tailwind frontend
│   ├── src/
│   ├── package.json
│   └── ...
├── backend/                           # Python backend (FastAPI)
│   ├── agents/
│   ├── scheduler/
│   ├── memory/
│   ├── pipeline/
│   ├── deploy/
│   └── requirements.txt
└── docs/
    ├── architecture-diagram.png       # Exported from TECHNICAL_SPEC
    └── demo-script.md                 # 3-minute demo script
```

---

## Source Files Read

| File | Description |
|---|---|
| `README.md` | Project front door — high-level overview |
| `HackathonBackground.md` | Hackathon description, tracks, judging criteria, API docs links |
| `what-is-kinora.md` | Plain-English explainer for non-technical readers |
| `rules.md` | Full official hackathon rules (418 lines) |
| `transcriptSaidFromTeammate.md` | Empty file (no content yet) |
| `Information/mdFile/kinora.md` | Full technical design document (1,030 lines, 72KB) |
| `Information/pdfFile/kinora.md.pdf` | PDF version of kinora.md (37 pages, same content) |
| `Information/pdfFile/Qwen Cloud AI Showrunner...pdf` | Original project plan from teammate (6 pages) |
