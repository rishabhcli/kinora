# KINORA — Tech Stack & Framework Recommendations

> Recommended frameworks, languages, and libraries for building Kinora. Each choice includes reasoning and alternatives.

---

## Recommended Stack Summary

| Layer | Technology | Why |
|---|---|---|
| **Frontend** | React 18 + TypeScript + Vite | Industry standard, huge ecosystem, excellent for complex state (SyncEngine) |
| **UI Styling** | TailwindCSS + shadcn/ui | Rapid development, consistent design, beautiful defaults |
| **PDF Rendering** | PyMuPDF (backend) + PDF.js (frontend) | PyMuPDF for extraction/rasterization; PDF.js for client-side virtualized rendering |
| **Backend API** | Python + FastAPI | Async-native, DashScope SDK is Python-first, excellent for AI workloads |
| **Agent Services** | Python microservices (FastAPI) | Each agent is a separate service with typed JSON contract |
| **MCP Server** | Python (FastAPI + MCP SDK) | Custom MCP server exposing canon tools via SSE |
| **Vector Store** | Alibaba Cloud OpenSearch or FAISS on ECS | OpenSearch for managed; FAISS for simplicity |
| **Canon Graph** | SQLite/PostgreSQL + JSON columns | Versioned graph with structured queries |
| **Object Storage** | Alibaba Cloud OSS | Required for hackathon; stores clips, frames, audio, canon vault |
| **Render Queue** | Alibaba Cloud MNS (Message Service) | Managed, reliable, integrates with ECS/Function Compute |
| **Real-time Events** | Server-Sent Events (SSE) | Simplest one-way push; upgrade to WebSocket if Director round-trips need it |
| **Video Player** | HTML5 `<video>` + custom canvas overlay | For Ken-Burns pan, karaoke highlight, seamless clip hot-swap |
| **Icons** | Lucide React | Clean, modern, tree-shakeable |
| **Deployment** | Alibaba Cloud ECS + Function Compute | Required by hackathon rules |

---

## Detailed Reasoning

### Frontend: React 18 + TypeScript + Vite

**Why React:**
- The SyncEngine is the most complex piece of frontend logic — bidirectional scroll↔video↔word binding with ownership tokens. React's component model + hooks handle this cleanly.
- Virtualised PDF page rendering (only visible pages in DOM) is well-supported with libraries like `react-virtuoso` or `@tanstack/react-virtual`.
- shadcn/ui provides beautiful, accessible components (split panes, segmented controls, dialogs) out of the box.
- TypeScript catches contract mismatches between frontend events and backend APIs at compile time.

**Why Vite (not Next.js):**
- Kinora is a single-page app (shelf → workspace), not a multi-page site. Vite is simpler, faster dev server, no SSR overhead.
- No SEO requirements (it's a hackathon demo app).
- If you need server-side rendering later, migrate to Next.js.

**Alternatives considered:**
- Vue 3 + Nuxt — equally capable, but React's ecosystem is larger for the specific libraries we need (PDF rendering, virtual scrolling, video manipulation).
- SvelteKit — excellent DX and performance, but smaller ecosystem for complex media handling.

### Backend: Python + FastAPI

**Why Python:**
- **DashScope SDK is Python-first.** The `dashscope` Python package is the official SDK. Video synthesis, text generation, TTS — all have first-class Python support.
- PyMuPDF (fitz) is Python-native for PDF extraction and rasterization.
- All AI/ML tooling (embeddings, vector search, CLIP-style similarity) is Python-native.
- FastAPI is async-native, supports WebSocket/SSE, auto-generates OpenAPI docs (good for documentation score).

**Why FastAPI (not Flask/Django):**
- Async support is critical — we're polling DashScope async tasks, pushing SSE events, handling concurrent render jobs.
- Pydantic models give us typed JSON contracts for free — the agent contracts in the design doc become Pydantic schemas.
- Automatic OpenAPI/Swagger docs = free architecture documentation for judges.

**Why microservices (not monolith):**
- The design explicitly calls for "each agent is a separately deployable service." This is a Track 3 requirement (Agent Society).
- But for the hackathon MVP, you can run all agents in a single FastAPI process with separate route modules — deploy as separate services later if time permits.

### MCP Server: Python + FastAPI + MCP SDK

**Implementation approach:**
- Build a custom MCP server using the `@modelcontextprotocol/sdk` Python equivalent or a simple FastAPI app that implements the MCP SSE protocol.
- Expose the 12 tools from the design doc (`canon.query`, `shot.render`, etc.) as MCP tool endpoints.
- The MCP server connects to the canon graph (SQLite/PostgreSQL) and episodic store (OpenSearch/FAISS).
- Agents connect to the MCP server via the Qwen Cloud Responses API with `tools=[mcp_tool]` configuration.

**Key detail:** MCP in Qwen Cloud is only supported via the **Responses API** (`client.responses.create`), not the standard Chat Completions API. The MCP server must use the SSE protocol.

### Vector Store: OpenSearch or FAISS

**OpenSearch (recommended for production):**
- Alibaba Cloud offers managed OpenSearch. Integrates natively with the Alibaba Cloud ecosystem.
- Supports vector search, filtering, and hybrid queries out of the box.
- Better for the "increasingly accurate across sessions" requirement — episodic memory grows over time.

**FAISS (recommended for MVP):**
- Simpler, no managed service needed. Run on ECS alongside the backend.
- Sufficient for the demo book's scale (a few hundred shots).
- Upgrade to OpenSearch if you need managed scaling.

### Canon Graph: SQLite or PostgreSQL

**SQLite (MVP):**
- Zero setup, file-based, perfect for a single-book demo.
- JSON1 extension supports querying into JSON columns (canon nodes are JSON).
- Versioning via `valid_from_beat` / `valid_to_beat` columns with simple SQL queries.

**PostgreSQL (production):**
- If you need concurrent access from multiple agent services.
- JSONB columns with GIN indexes for fast canon queries.
- Alibaba Cloud RDS for PostgreSQL if going managed.

### PDF Rendering

**Backend (extraction):** PyMuPDF (`fitz`)
- Extract text, images, layout, page dimensions
- Rasterize pages to images for Qwen3-VL analysis
- Get word bounding boxes for karaoke highlight overlay

**Frontend (display):** PDF.js or `react-pdf`
- Virtualized page rendering (only visible pages in DOM)
- Text layer overlay for word highlighting
- Scroll position tracking for focus word computation

**Alternative:** Render PDF pages as images on the backend (PyMuPDF) and display as `<img>` in the frontend. Simpler but loses text selection/search. For the MVP, this is acceptable and faster to implement.

### Video Player

**Custom HTML5 video + Canvas:**
- Standard `<video>` element for clip playback
- Canvas overlay for Ken-Burns pan (when only keyframe is available)
- Custom logic for seamless clip hot-swap (preload next clip in hidden `<video>`, switch on frame boundary)
- `requestAnimationFrame` loop for karaoke word highlighting synced to video time

**Libraries that can help:**
- `framer-motion` for smooth UI transitions
- `react-player` for video wrapper (but custom `<video>` may be simpler for the hot-swap logic)

### Real-time Transport: SSE → WebSocket

**SSE (Server-Sent Events) for MVP:**
- One-way push (backend → frontend) covers most events: `clip_ready`, `keyframe_ready`, `budget_low`, `agent_activity`
- Simpler than WebSocket, works through proxies
- FastAPI supports SSE natively with `StreamingResponse`

**WebSocket upgrade for Director mode:**
- Director mode needs round-trip: `comment{shot_id, region_png, note}` → backend → `regen_done` event
- Can start with REST POST for comments + SSE for responses, upgrade to WebSocket if latency matters

### Render Queue: Alibaba Cloud MNS

**Why MNS:**
- Managed message queue, no infrastructure to maintain
- Supports delayed messages, dead-letter queues, message priorities
- Workers on ECS/Function Compute pull jobs from MNS

**Alternative:** Redis + RQ (Redis Queue) if you want simpler setup for MVP. Deploy Redis on ECS. Migrate to MNS for the final deployment proof.

---

## Project Structure (Recommended)

```
QwenCloudHackathon/
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Shelf.tsx              # Bookshelf landing page
│   │   │   ├── Workspace.tsx          # Two-pane workspace
│   │   │   ├── PdfReader.tsx          # Virtualized PDF display
│   │   │   ├── VideoStage.tsx         # Video player + Ken-Burns
│   │   │   ├── SyncEngine.ts          # Playhead owner, w & v, intent push
│   │   │   ├── DirectorTools.tsx      # Region-select, timeline, canon editor
│   │   │   ├── BufferIndicator.tsx    # Faint hairline showing buffer fill
│   │   │   ├── AgentActivityFeed.tsx  # Live agent messages (conflicts, negotiations, decisions)
│   │   │   ├── ProductionDashboard.tsx # Track 4: budget burn, queue depth, error rate, HITL queue
│   │   │   ├── HitlCheckpoint.tsx     # Context-rich escalation cards with options + costs
│   │   │   └── MetricsPanel.tsx       # CCS + efficiency chart, crew vs. baseline
│   │   ├── hooks/
│   │   │   ├── useScrollSpy.ts        # Focus word computation
│   │   │   ├── useGenerationClient.ts # SSE/WS event handling
│   │   │   └── useWordHighlight.ts    # Karaoke highlight logic
│   │   ├── lib/
│   │   │   ├── api.ts                 # REST API client
│   │   │   └── types.ts               # Shared TypeScript types
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── tailwind.config.ts
│
├── backend/
│   ├── agents/
│   │   ├── showrunner.py              # qwen3.6-plus — planning, conflict arbitration
│   │   ├── adapter.py                 # qwen3.5-plus — PDF → screenplay → shot list
│   │   ├── continuity_supervisor.py   # qwen3.6-plus — canon writes, inconsistency flags
│   │   ├── cinematographer.py         # qwen3.6-plus (vision) — shot design
│   │   ├── generator.py               # wan2.7-i2v / happyhorse-1.0 + cosyvoice — render
│   │   ├── critic.py                  # qwen3.6-plus (vision) — QA scoring
│   │   └── production_manager.py      # qwen3.6-flash + qwen3.6-plus — Track 4 autopilot
│   ├── scheduler/
│   │   ├── controller.py              # Watermark buffer, promotion, cancel
│   │   ├── budget.py                  # Budget reserve/remaining + impact-ranked optimizer
│   │   ├── remediation.py             # Error recovery strategy table + execution
│   │   └── quality_gates.py           # Stage-level quality gate checks
│   ├── memory/
│   │   ├── mcp_server.py              # MCP SSE server exposing canon tools
│   │   ├── canon_graph.py             # Canon graph CRUD + versioning
│   │   ├── episodic_store.py          # Vector store for shot history
│   │   └── shot_cache.py              # Content-hash cache
│   ├── pipeline/
│   │   ├── ingest.py                  # Phase A: PDF → canon + shot list
│   │   ├── render.py                  # Phase B: shot spec → clip
│   │   ├── narrate.py                 # CosyVoice narration + sync map
│   │   ├── stitch.py                  # Scene concatenation
│   │   └── prompts.py                 # Version-controlled prompt templates
│   ├── deploy/
│   │   ├── alibaba_render_worker.py   # Proof-of-deployment artifact
│   │   ├── oss_utils.py               # OSS upload/download helpers
│   │   └── dashscope_client.py        # DashScope API wrapper
│   ├── api/
│   │   ├── routes.py                  # FastAPI routes (REST + SSE)
│   │   └── schemas.py                 # Pydantic models for all contracts
│   ├── main.py                        # FastAPI app entry point
│   └── requirements.txt
│
├── docs/
│   ├── architecture-diagram.png       # Exported from TECHNICAL_SPEC
│   └── demo-script.md                 # 3-minute demo script
│
├── LICENSE                            # MIT or Apache-2.0
├── README.md
├── PROJECT_OVERVIEW.md
├── TECHNICAL_SPEC.md
├── TECH_STACK.md                      # This file
├── HACKATHON_REQUIREMENTS.md
├── IMPROVEMENTS_AND_SUGGESTIONS.md
└── BUILD_ROADMAP.md
```

---

## Key Dependencies

### Frontend (`package.json`)

```json
{
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "typescript": "^5.4.0",
    "tailwindcss": "^3.4.0",
    "@radix-ui/react-dialog": "^1.1.0",
    "@radix-ui/react-separator": "^1.1.0",
    "lucide-react": "^0.400.0",
    "framer-motion": "^11.0.0",
    "react-virtuoso": "^4.7.0",
    "clsx": "^2.1.0",
    "tailwind-merge": "^2.3.0"
  },
  "devDependencies": {
    "vite": "^5.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "autoprefixer": "^10.4.0",
    "postcss": "^8.4.0"
  }
}
```

### Backend (`requirements.txt`)

```
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
pydantic>=2.7.0
dashscope>=1.20.0
oss2>=2.18.0
pymupdf>=1.24.0
numpy>=1.26.0
faiss-cpu>=1.8.0
httpx>=0.27.0
python-multipart>=0.0.9
sse-starlette>=2.1.0
```

---

## Why Not Other Languages?

### Node.js (considered)
- DashScope SDK has Node.js support, but it's less mature than Python.
- No PyMuPDF equivalent — would need to call Python as a subprocess for PDF extraction.
- AI/ML ecosystem (embeddings, vector search, CLIP) is Python-dominant.
- **Verdict:** Use Python for backend. Frontend is TypeScript anyway.

### Go (considered)
- Excellent for the render queue and scheduler (concurrency, performance).
- But DashScope SDK support is minimal. Would need raw HTTP calls.
- AI/ML ecosystem is nearly nonexistent in Go.
- **Verdict:** Overkill for a hackathon. Python's async is sufficient.

### Java (considered)
- DashScope Java SDK is well-supported (the API docs show Java examples).
- But development speed is slower than Python for prototyping.
- **Verdict:** Good for production, too slow for hackathon iteration.
