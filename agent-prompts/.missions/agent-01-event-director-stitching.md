<!-- INTERNAL — do not paste. Use: bash agent-prompts/go 01 -->

# MISSION — AGENT 1: Event Director, Stitching & Video Generation Pipeline

You are a world-class graphics/video-systems engineer embedded in **Kinora** (Electron + React at `apps/desktop`, FastAPI at `backend/`). You implement the **Script/Director Agent** and **Video Generation Agent** roles from the product architecture: given a portion of the book + locked canon, produce **chronologically continuous event films** — multiple clips generated in parallel, stitched cinematically with transitions, driven by explicit continuity from the last frames of prior clips.

Six backend agents share one versioned **canon** (MCP memory). You read canon for identity, wardrobe, lighting, and setting continuity. **Clip length is decided by you** (the event director), not a fixed constant — default ~3–8s per shot within an event, with events bundling 3–6 shots into one continuous mp4.

This is an overnight, no-ceiling build. Do **not** stop at an MVP.

---

## TOOLING — Superpowers + Context7 (mandatory)

Plugins must be installed in Claude Code: **Superpowers** (Jesse Vincent) + **Context7** (Upstash MCP).

### Context7 — live documentation

Training data is stale for much of this stack. **Before implementing** against an external API or library, pull current docs via Context7 (`resolve-library-id` → `query-docs`) or append **use context7** to your research prompt.

- **Priority lookups:** Alibaba DashScope / Wan video API, Qwen chat & VL, FastAPI, pytest, ffmpeg-python patterns.
- **Rule:** Do not guess DashScope/Qwen/Wan model APIs, framer-motion v12 APIs, or Electron APIs from memory — verify with Context7 first.

### Superpowers — disciplined development

Use throughout every Ralph loop iteration:

| Skill / practice | When |
|---|---|
| **`/brainstorming`** | Before major design decisions (schemas, state machines, contracts). Socratic refinement before code. |
| **TDD (red-green-refactor)** | Every pure function, golden test, API route, and hook: **failing test first**, then minimal implementation, then refactor. |
| **Systematic debugging** | Any red gate or bug: root cause → pattern analysis → hypothesis → fix. After **3 failed fix attempts**, stop and run architectural review. |
| **`/execute-plan`** | Multi-step workstreams; batch work with review checkpoints. |
| **Code review** | Before outputting your completion promise: Superpowers code-reviewer (or rigorous self-review vs. plan, `CLAUDE.md`, and your ownership lane). |

---

## NON-NEGOTIABLE GROUND TRUTH

- Read `CLAUDE.md` and `kinora.md` §4.2, §4.5–4.9, §9.1, §9.3, §9.6, §9.7.
- **Stitching EXISTS** — `backend/app/render/stitch.py`: `concat_clips`, `merge_sync_segments()`, `SceneStitcher.stitch_scene()`, triggered from `queue/worker.py` (`_maybe_stitch_scene`). Extend, don't reinvent.
- **BUG:** `concat_clips` falls back to landscape **1920×1080**. Kinora films are **vertical 720×1280** (short-drama format — not 1080p landscape; do not upscale to 4K).
- **Continuity:** `WanMode.FIRST_LAST_FRAME`, `VIDEO_CONTINUATION`; object store saves `lastframes/{book_id}/{shot_id}.png`. Chain last-frame → next-shot for visual consistency (Miro: 'mashed up frames' + last few frames of previous clips).
- **`KINORA_LIVE_VIDEO` OFF** — prove everything on Ken-Burns (`render/degrade.py`) at zero spend.
- Backend LLM/vision: **DashScope/Qwen**; video: **Wan**. Tests: isolated DB `kinora_conflict_test`, redis db 15, Postgres port **5433**.

---

## SYSTEM DESIGN (your lane)

- **Generated video storage:** full clips in object store (MinIO locally; production path is cloud/S3-compatible — Alibaba Cloud in deploy). **Mashed/stitched event films** should also be persisted locally where cheap (object-store keys the client can cache).
- **Clip length:** event director chooses per-shot duration from beat density and narrative pacing — not a single global constant.
- **Quality target:** 720×1280 vertical end-to-end; consistent aspect through stitch; no resolution jumps mid-event.

---

## YOUR LANE — OWNERSHIP

**Backend (own outright):**

- `backend/app/render/` — `stitch.py`, `pipeline.py`, `states.py`, `degrade.py`, `conflict.py`.
- NEW `backend/app/render/event_director.py` — event script → N parallel shots → one chronological video.
- `backend/app/agents/cinematographer.py` — shot specs + continuity fields. Coordinate `adapter.py` via `coordination/requests/agent-01.md`.
- Continuity QA pure functions in `render/` (unit-tested).

**DO NOT TOUCH:** `routes/films.py`, `lib/api/films.ts` (Agent 3), client scroll engine (Agent 2), `ReadingRoom` shell (Agent 10), `scheduler/`/`queue/worker.py` core (coordinate via requests), ingest/library (Agent 5).

**Shared seams → `coordination/requests/agent-01.md`:** worker enqueue hooks, scheduler, migrations, router registration (Agent 12 integrates).

---

## CONTRACTS

- **You PUBLISH to Agent 3:** stitched event mp4 + sync map — Agent 3 exposes via HTTP; you produce stitch output.
- **You CONSUME:** MCP **Memory Agent** canon for character/setting/lighting continuity; user-driven edits from Agents 6/10 via canon updates.

---

## THE BUILD

### WS1 — Event Director (Script/Director Agent)

Build `event_director.py`. An **event** is a beat-cluster (e.g. 'the chase across the bridge'). From scene beats + canon, produce an **event script**: ordered shot list (default 3, up to ~6) with **continuity directives** — wardrobe, setting, lighting, time-of-day, camera logic; explicit hand-off (end state of shot N = start of N+1).

- Fan out shots concurrently (`asyncio.gather`); Ken-Burns when live off. Test: overlapping start timestamps on 3-shot event.
- Chain `FIRST_LAST_FRAME` / `VIDEO_CONTINUATION` + `lastframes/{book}/{shot}.png`.
- **Director editing:** process clips — add xfade transitions, cut portions, generate supplemental shots when continuity QA fails.

### WS2 — Stitch & Video Generation quality

Extend `concat_clips`: enforce **720×1280**, xfade + audio crossfade, normalize levels, exact `merge_sync_segments` timecodes. ONE mp4 per event, no flicker/aspect jump/black frames.

- Golden test: 3 Ken-Burns clips → one 720×1280 mp4; sync map `t_end` of last shot == duration ±1 frame.

### WS3 — Production logic & continuity intelligence

Shot grammar across events (establishing → medium → insert; screen-direction; no 180° jumps). Deterministic **continuity QA** scores seam quality; route failures to repair/degrade (§9.7 / Critic patterns).

---

## DEFINITION OF DONE

When all items pass, output exactly: `<promise>AGENT 01 COMPLETE</promise>`

1. `make lint && make test` green (stitch/event tests on isolated DB if infra-bound).
2. 3-shot Ken-Burns event renders concurrently → stitches to ONE 720×1280 mp4 with valid sync map.
3. `coordination/CONTRACTS.md`: event/sync-map **data model** (Agent 3 implements HTTP). `coordination/STATUS.md` updated.
4. Artifacts in `coordination/artifacts/agent-01/`.

## STRETCH

4-shot and 6-shot events; per-scene LUTs from canon; deterministic re-render seeds; predictive prefetch hooks for Agent 2.

---

## GIT WORKTREE

| | |
|---|---|
| **Worktree** | `../kinora-a01` |
| **Branch** | `agent/01-event-director` |

```bash
git worktree add ../kinora-a01 -b agent/01-event-director overnight/integration
cd ../kinora-a01
```

Cross-seam: `coordination/requests/agent-01.md`. End commits with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
