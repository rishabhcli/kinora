# KINORA — Build Roadmap (18 Days)

> Ship the core loop on one book, not the whole vision. The goal is a 3-minute demo that makes a judge feel something.

---

## Timeline Overview

| Phase | Days | Focus | Deliverable |
|---|---|---|---|
| **Phase 1: Foundation** | Days 1–4 | Project setup, PDF rendering, SyncEngine | Two-pane workspace with PDF↔video sync |
| **Phase 2: Memory + Agents** | Days 5–9 | Canon graph, MCP server, 6 creative agents + Production Manager | Canon populated, agents responding, autopilot quality gates running |
| **Phase 3: Generation** | Days 10–14 | Render pipeline, Scheduler, Critic loop, remediation engine | Full per-shot pipeline with automated error recovery |
| **Phase 4: Director + Polish** | Days 15–17 | Director mode, HITL checkpoints, metrics, agent feed, production dashboard | All demo features working across 4 tracks |
| **Phase 5: Submit** | Day 18 | Demo video, deployment proof, submission | Everything on Devpost |

---

## Phase 1: Foundation (Days 1–4)

### Day 1 — Project Setup & Infrastructure

**Tasks:**
- [ ] Initialize git repo, add MIT LICENSE file, set repo description
- [ ] Create `.env.example` with all required environment variables:
  ```
  DASHSCOPE_API_KEY=sk-xxx
  OSS_AK=your-access-key
  OSS_SECRET=your-secret
  OSS_ENDPOINT=https://oss-ap-southeast-1.aliyuncs.com
  OSS_BUCKET=kinora-assets
  ```
- [ ] Scaffold frontend: `npm create vite@latest frontend -- --template react-ts`
- [ ] Install TailwindCSS, shadcn/ui, Lucide icons, framer-motion
- [ ] Scaffold backend: FastAPI project structure (see TECH_STACK.md)
- [ ] Install Python dependencies: `fastapi`, `dashscope`, `oss2`, `pymupdf`, `faiss-cpu`, `ffmpeg-python`
- [ ] Set up Alibaba Cloud OSS bucket (Singapore region)
- [ ] Test DashScope API connectivity with a simple `qwen3.6-plus` call
- [ ] Pick the demo book (e.g., "The Snow Queen" or "Little Red Riding Hood")

**Deliverable:** Empty project that builds and connects to DashScope + OSS.

### Day 2 — PDF Rendering & Shelf UI

**Tasks:**
- [ ] Backend: PyMuPDF PDF extraction endpoint (upload → extract text, images, page dimensions, word bounding boxes)
- [ ] Frontend: Shelf/landing page with book grid (Apple Books style)
- [ ] Frontend: PDF reader component with virtualized page rendering
- [ ] Frontend: Split-pane workspace layout (PDF left, video right)
- [ ] Frontend: Liquid-glass Viewer/Director segmented control

**Deliverable:** Upload a PDF, see it rendered in the left pane with virtualized scrolling.

### Day 3 — SyncEngine (The Hard Part)

**Tasks:**
- [ ] Frontend: ScrollSpy — compute focus word `w` from scroll position (top third of viewport)
- [ ] Frontend: Reading velocity `v` — EWMA over 10s window, clamped [0.5×, 3×]
- [ ] Frontend: Video player component with seamless clip hot-swap (preload in hidden `<video>`)
- [ ] Frontend: SyncEngine — bidirectional scroll↔video binding with control-owner token (1.2s grace)
- [ ] Frontend: Word highlight layer (karaoke) — canvas overlay highlighting current word
- [ ] Frontend: Ken-Burns pan effect on canvas (for keyframe fallback)
- [ ] Backend: WebSocket endpoint for real-time events

**Deliverable:** Two-pane workspace where scrolling PDF seeks video and playing video turns PDF pages. No generation yet — use a pre-made dummy video.

### Day 4 — Phase A Ingest Pipeline + Production Autopilot Intake

**Tasks:**
- [ ] Backend: Ingest pipeline — PDF → PyMuPDF extraction → Qwen3-VL page analysis
- [ ] Backend: Canon graph initialization (SQLite with JSON columns)
  - Character nodes (name, appearance description, aliases)
  - Location nodes
  - Prop nodes
  - Style node (palette, lens, art direction)
  - Continuity-state nodes
- [ ] Backend: Source-span index builder (word index → shot mapping)
- [ ] Backend: Shot list generation (Adapter agent: beats → shots with source spans)
- [ ] Backend: Canon keyframe generation (image-gen for character reference images)
- [ ] Backend: CosyVoice voice cloning for each character
- [ ] Frontend: Ingest progress indicator on book cover ("preparing… 60%")
- [ ] **Track 4:** Production Manager intake & triage — `qwen3.6-flash` classifies PDF (genre, complexity, character count, visual richness), runs content safety pre-scan, estimates cost (video-seconds + tokens), allocates budget across scenes by narrative impact

**Deliverable:** Upload demo book → Phase A runs → canon graph populated, shot list created, character reference images generated, voices cloned. Production Manager has classified the book, estimated costs, and allocated budget. No video yet.

---

## Phase 2: Memory + Agents (Days 5–9)

### Day 5 — MCP Canon Server

**Tasks:**
- [ ] Backend: Build MCP server (FastAPI + SSE protocol)
- [ ] Implement MCP tools:
  - `canon.query(beat_id)` → returns canon slice (characters present + location + style + last frame)
  - `canon.get_entity(id, at_beat?)` → versioned entity resolution
  - `canon.upsert_entity(entity)` → write new version
  - `canon.assert_state(subject, predicate, object, valid_from)` → add versioned fact
  - `canon.retire_state(state_id, valid_to)` → forgetting
- [ ] Test MCP server with Qwen Responses API (`client.responses.create` with `tools=[mcp_tool]`)
- [ ] Verify agents can query the canon through MCP

**Deliverable:** MCP server running, agents can query canon through Qwen Cloud's Responses API.

### Day 6 — Agent Services (Showrunner + Adapter + Continuity)

**Tasks:**
- [ ] Backend: Showrunner agent (`qwen3.6-plus`) — scene planning, book decomposition, conflict arbitration
- [ ] Backend: Adapter agent (`qwen3.5-plus`) — screenplay → shot list with source spans
- [ ] Backend: Continuity Supervisor (`qwen3.6-plus`) — canon writes, inconsistency detection, versioning
- [ ] Define Pydantic schemas for all agent contracts (request/response types)
- [ ] Implement agent-to-agent communication via typed JSON contracts
- [ ] Backend: Conflict detection and structured conflict objects

**Deliverable:** Three agents running, can decompose the demo book into scenes/beats/shots, populate and validate the canon.

### Day 7 — Agent Services (Cinematographer + Generator + Critic + Production Manager)

**Tasks:**
- [ ] Backend: Cinematographer agent (`qwen3.6-plus` vision) — shot spec generation (prompt, refs, camera, seed, Wan mode)
- [ ] Backend: Generator agent — Wan 2.7 / HappyHorse video generation + CosyVoice narration
  - Implement Wan-mode decision tree
  - Async task submission + polling
  - Word timestamp extraction from CosyVoice
- [ ] Backend: Critic agent (`qwen3.6-plus` vision) — QA scoring (CCS, style drift, timeline, motion artifact)
- [ ] Backend: Self-correcting loop (Critic → repair → retry ≤ 2 → degrade)
- [ ] **Track 4:** Production Manager agent (`qwen3.6-flash` + `qwen3.6-plus`) — quality gates at each stage, automated proceed/escalate decisions, logging with reasoning

**Deliverable:** All 7 agents running. Can generate a single shot end-to-end: spec → render → QA → accept/reject. Production Manager monitors quality gates and logs decisions.

### Day 8 — Episodic Store + Caching + Budget Optimizer

**Tasks:**
- [ ] Backend: Episodic vector store (FAISS) — embed every shot record for retrieval
- [ ] Backend: `episodic.search(embedding, filters)` — "what worked before" retrieval
- [ ] Backend: `episodic.log(shot_record)` — write QA + outcome
- [ ] Backend: Shot cache (content-hash keyed) — `shot_hash = sha1(book_id + beat_id + canon_version + render_mode + seed + ref_set_hash)`
- [ ] Backend: Budget service — `budget.reserve(seconds)`, `budget.remaining()`, per-session allocation
- [ ] Backend: `prefs.get` / `prefs.upsert` — Director preference storage
- [ ] **Track 4:** Budget optimizer — impact ranking (climax > confrontation > dialogue > transition), real-time reallocation of surplus, cost-benefit computation for Director edits

**Deliverable:** Full memory layer operational — cache hits serve from OSS for zero video-seconds, budget tracking + optimization works.

### Day 9 — Agent Integration Test

**Tasks:**
- [ ] End-to-end test: upload demo book → Phase A → generate 3 shots → QA → cache
- [ ] Verify canon consistency across shots (same character looks the same)
- [ ] Test cache hit path (re-request same shot → 0 video-seconds)
- [ ] Test budget guardrails (budget exhausted → degradation ladder)
- [ ] Fix bugs discovered in integration
- [ ] Frontend: Display agent activity log (simple scrolling text feed via WebSocket)

**Deliverable:** Complete agent pipeline working on the demo book. 3 shots generated, QA-passed, cached.

---

## Phase 3: Generation-on-Scroll (Days 10–14)

### Day 10 — Scheduler / Prefetch Controller

**Tasks:**
- [ ] Backend: Scheduler service — session state, control loop
- [ ] Implement watermark buffer logic (L=25s/40s, H=75s, hysteresis)
- [ ] Implement velocity-adaptive promotion (ETA < C=45s, trajectory stable, budget ok)
- [ ] Implement debounce (200ms), dwell confirmation, idle-pause (8s)
- [ ] Backend: Intent update endpoint (WebSocket: `intent_update{session_id, focus_word, velocity, mode}`)

**Deliverable:** Scheduler receives scroll events and decides what to render. Buffer fills in bursts, goes idle between.

### Day 11 — Render Queue + Workers

**Tasks:**
- [ ] Backend: Render queue (Alibaba Cloud MNS or Redis + RQ)
- [ ] Implement idempotency keys (`shot_hash`)
- [ ] Implement cancellation tokens (cooperative cancel at safe points)
- [ ] Implement exponential-backoff retries (2s, 8s, 30s)
- [ ] Implement dead-letter queue → degradation fallback
- [ ] Backend: Worker service (pulls jobs, calls DashScope, writes to OSS, triggers Critic)
- [ ] Concurrency caps: 4 committed + 2 speculative + keyframe pool
- [ ] Backpressure: drop speculative enqueues when saturated

**Deliverable:** Render queue processing jobs with retries, cancellation, and DLQ. Workers pulling from queue and generating video.

### Day 12 — Sync Map + Video Stitching

**Tasks:**
- [ ] Backend: Sync map builder — CosyVoice word timestamps → sync segments (video_time ↔ page ↔ word ↔ bbox)
- [ ] Backend: Scene stitching — `ffmpeg` concatenation of accepted shots
- [ ] Backend: Audio normalization across clips
- [ ] Frontend: Consume `clip_ready` events → preload + hot-swap video
- [ ] Frontend: Consume `scene_stitched` events → switch to stitched scene playback
- [ ] Frontend: Buffer indicator (faint hairline showing committed-seconds-ahead / H)

**Deliverable:** Reader scrolls → shots generate ahead → clips hot-swap seamlessly → karaoke highlight syncs → page turns automatically.

### Day 13 — Seek/Skip + Degradation + Remediation Engine

**Tasks:**
- [ ] Backend: Seek handler — cancel distant speculative, bridge with keyframe, re-seed
- [ ] Backend: Degradation ladder — full video → Ken-Burns keyframe → book illustration → narrated text
- [ ] Backend: Budget-aware degradation (`budget_low` event → stop promoting to full video)
- [ ] **Track 4:** Remediation engine — strategy table for each failure type (api_timeout, content_rejected, ccs_fail, budget_exhausted), automated recovery with escalation to HITL only when all strategies exhausted
- [ ] **Track 4:** Content safety pipeline — prompt sanitization for DashScope content policy, rejection pattern logging to episodic memory
- [ ] Frontend: Ken-Burns pan when only keyframe available (canvas animation)
- [ ] Frontend: Handle `budget_low` event → quiet notice
- [ ] Test: fast skim → no video wasted; seek → instant bridge; idle → no generation
- [ ] Test: inject API timeout → verify remediation engine auto-recovers (switch model, retry, degrade)

**Deliverable:** Generation-on-scroll robust against all failure modes. Remediation engine auto-recovers from errors without human intervention.

### Day 14 — Full Pipeline Integration Test

**Tasks:**
- [ ] End-to-end test on one full chapter of the demo book (~60-90s of video)
- [ ] Verify: ingest → keyframe → video → narration → critic → stitch → display
- [ ] Verify: scroll forward → buffer fills → clips play seamlessly
- [ ] Verify: scroll back → cache hit → instant replay
- [ ] Verify: seek → keyframe bridge → full video catches up
- [ ] Fix all bugs
- [ ] Pre-render the first 3-5 shots of the demo book (so demo opens with full buffer)

**Deliverable:** Complete generation-on-scroll working on one chapter. Ready for Director mode.

---

## Phase 4: Director + Polish (Days 15–17)

### Day 15 — Director Mode + HITL Checkpoints

**Tasks:**
- [ ] Frontend: Region-select on video frame (drag box, screenshot region)
- [ ] Frontend: Comment composer (natural language note + region image)
- [ ] Backend: Comment routing (intent classifier → route to correct agent)
- [ ] Backend: Canon update from Director edit → compute dependent shots → surgical re-render
- [ ] Frontend: Shot timeline / filmstrip with QA badges
- [ ] Frontend: Canon editor (inspectable + editable canon graph)
- [ ] Frontend: `regen_done` event → swap single shot on screen
- [ ] **Track 4:** HITL checkpoint UI — context-rich escalation cards (what happened, what was tried, options, costs), async resume (pipeline continues while waiting), decision logging
- [ ] **Track 4:** Budget override checkpoint — "This edit costs 15 video-seconds, remaining: 847s. Proceed?" with accept/modify/cancel

**Deliverable:** Director can region-select, comment, and trigger surgical re-render. HITL checkpoints are meaningful and context-rich. Budget tradeoffs are explicit.

### Day 16 — Production Dashboard + Agent Feed + Conflict + Autopilot Demo

**Tasks:**
- [ ] Frontend: Metrics panel — CCS chart, accepted-footage efficiency, regeneration rate
- [ ] Frontend: Buffer-occupancy sawtooth visualization (over time)
- [ ] Frontend: Agent activity feed — streaming log of agent messages and conflict resolutions
- [ ] Frontend: **Production dashboard** — real-time budget burn, render queue depth, error rate, HITL queue, audit trail
- [ ] Backend: Single-agent baseline (minimal: same prompts, no canon, no critic) — or prepare conceptual comparison
- [ ] Set up the demo conflict (e.g., "lost sword" — character draws a sword they lost earlier)
- [ ] Verify conflict detection → Showrunner arbitration → live in agent feed
- [ ] **Track 4:** Set up the autopilot demo — inject a failure (e.g., content policy rejection) → show remediation engine auto-recovering in the agent feed → show HITL checkpoint for budget override
- [ ] Backend: Production report endpoint — cost breakdown, quality metrics, production timeline, audit trail

**Deliverable:** Production dashboard showing all 4 tracks in action. Agent feed showing live conflict resolution + automated error recovery. Metrics panel showing crew vs. baseline.

### Day 17 — Polish + Demo Prep

**Tasks:**
- [ ] UI polish: transitions, loading states, empty states, error states
- [ ] Keyboard shortcuts (arrow keys, space, tab to switch Viewer/Director)
- [ ] Cinema mode toggle (fullscreen video)
- [ ] Pre-generate and cache the entire demo sequence (so demo is reliable even if DashScope is slow)
- [ ] Write the 3-minute demo script (see below)
- [ ] Practice the demo 3+ times
- [ ] Record the Alibaba Cloud deployment proof video
- [ ] Export architecture diagram as clean PNG
- [ ] Write text description for Devpost submission

**Deliverable:** Everything polished and ready to record.

---

## Phase 5: Submit (Day 18)

### Day 18 — Final Submission

**Tasks:**
- [ ] Record the 3-minute demo video
- [ ] Upload demo video to YouTube (public)
- [ ] Write and publish a blog post about the build journey (optional, for Blog Post Prize)
- [ ] Final Devpost submission:
  - [ ] Public repo URL with LICENSE visible in About
  - [ ] Text description of features + functionality
  - [ ] Architecture diagram
  - [ ] Demo video URL (YouTube)
  - [ ] Proof of Alibaba Cloud deployment (link to `deploy/alibaba_render_worker.py` + recording)
  - [ ] Track identified: Track 2 — AI Showrunner (with coverage of T1, T3, T4)
  - [ ] Testing instructions (how to run the project)
  - [ ] Optional: Blog post URL
- [ ] Verify all submission requirements are met
- [ ] Submit before 2:00pm PDT

**Deliverable:** Everything submitted on Devpost before deadline.

---

## Demo Script (3 Minutes)

| Time | Section | Track | What to show |
|---|---|---|---|
| 0:00–0:25 | **The hook** | T2 | "You don't read it — you watch it." Viewer mode playing, words highlighting, page turning itself. Land the accessibility angle. "None of this existed five seconds ago — it's generating as I read." |
| 0:25–1:10 | **Generation-on-scroll** | T2 | Scroll forward; show buffer hairline filling in bursts then going quiet. Scroll fast → Ken-Burns bridge → full video catches up. |
| 1:10–1:50 | **Director mode** | T1, T2 | Region-select a character, "make her coat crimson," watch canon update and only that shot regenerate. **Budget cost displayed: "3 shots, 15 video-seconds."** |
| 1:50–2:15 | **Agent negotiation** | T3 | Live agent feed: continuity conflict (lost sword) → Showrunner arbitration → resolution. Agents visibly negotiating. |
| 2:15–2:40 | **Autopilot in action** | T4 | Production Manager: quality gate catches a bad shot → remediation engine auto-recovers (switches Wan mode) → HITL checkpoint for budget override → judge sees automated decision log. |
| 2:40–3:00 | **Metrics + vision** | T1-4 | CCS chart (crew vs. baseline), buffer sawtooth, production report. "Any book, any reader, any attention span." Close. |

---

## Risk Mitigation Summary

| Risk | Mitigation |
|---|---|
| Not enough time | Cut stretch features. Ship the MVP loop only. |
| DashScope latency too high | Pre-render demo content. Increase L to 40s. |
| CosyVoice timestamps imprecise | Fall back to sentence-level highlighting. |
| CCS threshold wrong | Calibrate early with 10 test clips. |
| MCP server too complex | Fall back to function calling (Plan B). |
| Demo fails live | Pre-cache everything. Have a cached-only fallback. |
| Budget runs out | Pre-render demo sequence. Use batch API. Image-only speculation. |

---

## Stretch Features (Only If MVP Is Solid)

- Full pointer-commenting UX with all routing paths
- Multi-scene films (beyond one chapter)
- Manga overlay mode
- Cross-session preference learning surfaced in UI
- Full velocity-adaptive promotion tuning
- EPUB support
- Cinema mode
- Regeneration cost display
- Blog post for Blog Post Prize
