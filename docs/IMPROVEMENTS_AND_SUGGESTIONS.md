# KINORA — Improvements, Gaps & Suggestions

> Critical analysis of the current design. What's missing, what's risky, what could be better, and what the team might not have considered.

---

## Critical Gaps (Must Fix Before Building)

### 1. Model Names Are Wrong Throughout the Design

The design doc uses "Qwen3.7-Max", "Qwen3.7-Plus", "Qwen3.5-Plus", and "Qwen3-VL" — **none of these are actual API model names.** The verified model names from Alibaba Cloud documentation are:

| Design Doc Name | Actual API Model Name |
|---|---|
| Qwen3.7-Max | `qwen3.6-plus` (qwen3-max is LEGACY) |
| Qwen3.7-Plus | `qwen3.5-plus` |
| Qwen3.5-Plus | `qwen3.5-plus` |
| Qwen3-VL | `qwen3.6-plus` (vision is built-in, qwen3-vl-plus is LEGACY) |

**Impact:** If the code uses the wrong model names, every API call fails. Fix all references in code and documentation before writing any agent code.

### 2. MCP Integration Path Is Unclear

The design says "exposed to every agent as an MCP server" and "wire `canon.query` and `shot.render` as custom Qwen skills." But the actual Qwen Cloud MCP integration works differently:

- **MCP is only supported via the Responses API** (`client.responses.create`), not the standard Chat Completions API
- MCP servers must use the **SSE protocol**
- You register MCP tools in the `tools` parameter with `type: "mcp"`, `server_protocol: "sse"`, `server_url`, and `headers`
- Maximum 10 MCP servers per request

**What this means:** You need to either:
- **(A)** Build a custom MCP server (FastAPI + SSE) that exposes the canon tools, then register it with Qwen's Responses API — this is the "sophisticated MCP integration" the judges want
- **(B)** Use function calling (tool calling) via the Chat Completions API instead, which is simpler but scores fewer points on the "MCP integration" criterion

**Recommendation:** Go with (A). Build the MCP server. It's a hard requirement for maximum Technical Depth scores. The MCP server is a FastAPI app that implements the SSE protocol and exposes the 12 canon tools.

### 3. No Code Exists Yet

The entire project is design documentation. Zero implementation has started. With an 18-day build plan and a Jul 9 deadline, this is tight. The design is thorough, but the risk is spending too much time on the design's complexity instead of shipping the MVP loop.

### 4. `transcriptSaidFromTeammate.md` Is Empty

This file is 0 bytes. If there are notes from a teammate discussion, they're missing. This could contain important context about decisions, constraints, or分工.

### 5. No LICENSE File

The hackathon **requires** an open-source license visible in the repo's About section. This is a hard submission requirement. Add MIT or Apache-2.0 immediately.

### 6. The Teammate's PDF Plan Differs from kinora.md

The PDF "Qwen Cloud AI Showrunner PDF to Video Adaptation System.pdf" (from a teammate) describes a simpler architecture:
- Uses **LangGraph** for HITL workflows (not mentioned in kinora.md)
- Uses **Microsoft Agent Framework** (not mentioned in kinora.md)
- Proposes a 21+ week timeline (way too long for a hackathon)
- Does NOT mention generation-on-scroll, the watermark buffer, or the canon graph
- Does NOT mention MCP

**Risk:** If the team is split between two architectures, you'll waste time. **kinora.md is the superior design** — it's more novel, more technically deep, and explicitly designed for the hackathon constraints. The teammate's PDF should be treated as an early brainstorm, not the spec.

---

## Technical Risks & Mitigations

### 7. Video Generation Latency Is Underestimated

The design assumes 30–90s wall-clock per 5s clip. In practice:
- Wan 2.7 async tasks can take **1–5 minutes** (per Alibaba Cloud docs)
- HappyHorse 1.0 similarly
- During peak load, DashScope may queue requests

**Mitigation:**
- Increase the watermark buffer's low watermark `L` from 25s to 40s for the demo
- Pre-render the first 2-3 shots of the demo book before the reader opens it (so the demo starts with a full buffer)
- Have the Ken-Burns fallback polished — it will be used more than the design assumes

### 8. CosyVoice Word Timestamps May Not Be Precise Enough

The karaoke highlight depends on per-word timestamps from CosyVoice. If timestamps are imprecise (off by 100ms+), the highlight will feel desynced from the audio, which is visually jarring.

**Mitigation:**
- Test CosyVoice word timestamps early in the build
- If imprecise, fall back to sentence-level highlighting (less precise but still useful)
- Consider aligning timestamps with forced alignment (e.g., `whisper` or `aeneas`) as a post-processing step

### 9. CCS Threshold (0.85) Is Arbitrary

The Character Consistency Score threshold of 0.85 is stated as a concrete number, but it hasn't been validated. If CLIP-style embeddings don't separate well at that threshold, you'll either reject too many good clips or accept too many bad ones.

**Mitigation:**
- Run a calibration test early: generate 10 clips of the same character, compute CCS, see the distribution
- Adjust the threshold based on data, not assumption
- The design says "pre-register the thresholds" — do this before running the eval

### 10. The Canon Graph Is Complex to Build in 18 Days

The versioned canon graph with time-travel reads, forgetting, and reference-set hashing is a sophisticated piece of engineering. Building it from scratch in 18 days alongside everything else is risky.

**Mitigation:**
- Start with a **simplified canon**: flat JSON files per character/location/prop, no versioning, no time-travel
- Add versioning only if the demo needs it (the "lost sword" conflict demo does)
- Use SQLite with JSON columns — don't build a custom graph database
- The MCP server can start as a simple FastAPI app with in-memory data, backed by SQLite

### 11. No Error Handling for DashScope Rate Limits

The design mentions retries and DLQ but doesn't address **rate limiting**. DashScope has per-model rate limits (requests per minute, tokens per minute). With 6 agents calling simultaneously, you could hit limits fast.

**Mitigation:**
- Implement a token bucket rate limiter per model in the DashScope client wrapper
- Use `qwen3.5-flash` for cheap routing/classification tasks to reduce load on expensive models
- Batch API for Phase A analysis (the design mentions this — actually do it)

### 12. The Eval Harness Baseline Is Extra Work

Building a single-agent baseline (one `qwen3.6-plus` doing everything with no memory) just for the comparison chart is significant additional work. It's a strong differentiator but costs 2-3 days.

**Mitigation:**
- Make the baseline minimal: same prompts, no canon, no critic, just generate → accept
- Reuse the same pipeline code with memory disabled via a feature flag
- If time is short, present the baseline as a conceptual comparison with manual examples rather than a full automated run

---

## Improvements & Enhancements

### 13. Add a "Quick Start" Mode for Judges

Judges have limited time. If the demo requires uploading a PDF and waiting for Phase A analysis, that's 30-60 seconds of dead air. 

**Suggestion:** Pre-ingest the demo book and cache the canon + first 3 shots. When a judge clicks the book, it opens instantly with video already playing. Show the ingest process as a sped-up recording in the demo video, not live.

### 14. Add Keyboard Shortcuts

For the demo, keyboard shortcuts (arrow keys to turn pages, space to play/pause) make the interaction feel polished and professional. Judges notice smooth UX.

### 15. Consider a "Narration-Only" Fallback Mode

If video generation is completely down or budget is exhausted, the app should still deliver value: narration + karaoke highlight + page-turn sync. This is the bottom rung of the degradation ladder but it should be a **first-class feature**, not just a fallback. It's the core accessibility feature (dyslexia/ADHD aid) and works with zero video budget.

### 16. Add a "Regeneration Cost" Display

When a Director edit triggers a re-render, show the cost: "This edit will regenerate 3 shots (15 video-seconds). Remaining budget: 1,287s." This makes the budget-awareness visible and impressive to judges.

### 17. The Agent Activity Feed Should Be a Priority, Not a Stretch

The design marks the live agent-activity feed as "stretch." But it's the **single most visual differentiator** for Track 3 (Agent Society). Judges need to *see* agents negotiating. Move it to MVP.

**Minimal implementation:** A simple SSE stream of agent messages (JSON lines) rendered as a scrolling log in the UI. No fancy visualization needed — just text with timestamps and agent names. 2-3 hours of work, huge demo impact.

### 18. Add WebSocket Support from the Start

SSE is one-way. Director mode needs round-trip communication (comment → processing → regen_done). Starting with SSE and migrating to WebSocket later is technical debt. Use WebSocket from day 1 — FastAPI supports it natively.

### 19. Consider EPUB Support

The design mentions PDF only. But many public-domain books are available as EPUB (Project Gutenberg). Adding EPUB support is trivial with `ebooklib` in Python and expands the demo book options significantly.

### 20. The Demo Book Choice Is Critical

The design says "short, public-domain, illustrated story" but doesn't pick one. This decision drives everything downstream. 

**Recommendations:**
- **"The Snow Queen" by Hans Christian Andersen** — short, vivid imagery, few characters, public domain, strong visual scenes
- **"Little Red Riding Hood" (Brothers Grimm)** — very short, 2-3 characters, iconic visuals
- **An original 2-3 page short story** — zero copyright risk, you control the content
- **Aesop's Fables** — very short, simple narratives, public domain

**Avoid:** Anything still in copyright. Anything too long (>20 pages). Anything with too many characters (>4).

### 21. Add a "Cinema Mode" Toggle

A fullscreen video-only mode that hides the PDF pane. This gives a cleaner demo experience when you want to show off the video quality without the text distraction. Simple CSS toggle, high demo value.

---

## What You Might Be Missing

### 22. No Authentication / Session Management

The design doesn't mention user authentication or session management. For the hackathon demo, this is fine (single user, no auth). But if you want the "cross-session preference learning" feature to work, you need at least a session ID that persists across browser sessions (localStorage + backend session store).

### 23. No CI/CD Pipeline

For a hackathon, CI/CD is overkill. But a simple `Makefile` or `justfile` with commands like `make dev`, `make build`, `make deploy` helps judges run the project. Include a `docker-compose.yml` for local development — judges love one-command setup.

### 24. No Environment Variable Management

The design mentions `DASHSCOPE_API_KEY`, `OSS_AK`, `OSS_SECRET` but doesn't have a `.env.example` file. Create one immediately so the team knows what environment variables are needed.

### 25. No Testing Strategy

The design has an eval harness for the AI metrics (CCS, efficiency) but no unit tests or integration tests for the software itself. The SyncEngine (bidirectional scroll↔video binding) is the most bug-prone component and needs tests.

**Minimum viable testing:**
- Unit test the ETA calculation: `(shot.word_range.start - w) / v`
- Unit test the watermark buffer logic (low/high watermark transitions)
- Integration test the cache hit/miss path
- E2E test: upload PDF → Phase A → first clip renders

### 26. No Monitoring / Logging Strategy

The design mentions observability metrics but not how they're collected. For the demo, a simple structured logging setup (Python `structlog` or `loguru`) writing to stdout is sufficient. The metrics panel can read from an in-memory stats collector.

### 27. Consider Content Safety

DashScope may reject prompts that contain violence, adult content, or other policy-violating material. If the demo book has any such scenes (Grimm fairy tales can be dark), the pipeline will fail on those shots. Pre-screen the demo book's content against DashScope's content policy.

### 28. The PDF Mentioned LangGraph — Should We Use It?

The teammate's PDF mentions LangGraph for HITL workflows. kinora.md doesn't. 

**Recommendation:** Don't use LangGraph. The agent orchestration in kinora.md is custom (typed JSON contracts, MCP server, Scheduler). LangGraph adds a framework dependency and abstraction layer that doesn't match the design. The "HITL" requirement is satisfied by Director mode, which is a UI feature, not a workflow framework feature.

### 29. Consider Function Calling as Fallback for MCP

If the custom MCP server proves too complex to build in time, you can fall back to OpenAI-compatible function calling (tool calling) via the Chat Completions API. This is simpler and still demonstrates "sophisticated API use" — just not the MCP-specific integration the judges reward. Have this as a Plan B.

### 30. The Design Doesn't Address Video Stitching Details

"Accepted shots in a scene are concatenated" — but how? `ffmpeg` is the obvious answer, but the design doesn't mention it. You need:
- `ffmpeg` for shot concatenation (re-encoding at cut points)
- Audio normalization across clips (CosyVoice output may vary in volume)
- Transition handling (hard cut vs. crossfade — hard cut is simpler and fine for MVP)

**Add `ffmpeg-python` to requirements.txt.**

### 31. No Fallback for When DashScope Is Down

If DashScope has an outage during judging (Jul 10-31), the demo breaks entirely. Have a cached version of the demo running with pre-generated clips that can be served even if DashScope is unreachable. The shot cache (OSS) makes this possible — just serve everything from cache.

---

## Summary: Top 10 Priority Actions

1. **Fix all model names** to use actual API identifiers (`qwen3.6-plus`, `qwen3.5-plus`, `qwen3.6-flash`, etc.) — see [`ALIBABA_CLOUD_MODELS.md`](./ALIBABA_CLOUD_MODELS.md)
2. **Add LICENSE file** (MIT) — required for submission
3. **Pick the demo book** — drives everything downstream
4. **Build the MCP server** — it's the Technical Depth differentiator
5. **Move agent activity feed to MVP** — it's the Track 3 visual proof
6. **Pre-render demo content** — don't rely on live generation for the demo
7. **Test CosyVoice word timestamps early** — the karaoke feature depends on them
8. **Add `ffmpeg` to the stack** — needed for video stitching
9. **Create `.env.example`** — so the team can set up quickly
10. **Start coding immediately** — 18 days is tight with zero code written
