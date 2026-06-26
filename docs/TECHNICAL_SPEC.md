# KINORA — Technical Specification

> Full technical architecture with verified API details. This document consolidates the design from `kinora.md` with verified Qwen Cloud / DashScope API information.

---

## Table of Contents

1. [Verified Model Stack & API Details](#1-verified-model-stack--api-details)
2. [System Architecture](#2-system-architecture)
3. [Generation-on-Scroll](#3-generation-on-scroll)
4. [The Crew — Agents & Negotiation Protocol](#4-the-crew--agents--negotiation-protocol)
5. [The Memory Layer — MCP Canon Server](#5-the-memory-layer--mcp-canon-server)
6. [The Generation Pipeline](#6-the-generation-pipeline)
7. [Prompt Contracts](#7-prompt-contracts)
8. [Budget Accounting](#8-budget-accounting)
9. [Engineering — Reliability & Infrastructure](#9-engineering--reliability--infrastructure)
10. [Metrics & Eval Harness](#10-metrics--eval-harness)
11. [Deployment on Alibaba Cloud](#11-deployment-on-alibaba-cloud)

---

## 1. Verified Model Stack & API Details

### API Configuration

| | |
|---|---|
| **Base URL (Singapore)** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| **Base URL (US Virginia)** | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` |
| **Protocol** | OpenAI-compatible (use OpenAI SDK in Python/Node.js) |
| **Auth** | `Authorization: Bearer sk-xxx` |
| **MCP support** | Via Responses API only (`client.responses.create`), SSE protocol, max 10 MCP servers per request |

### Model Mapping (Design → Actual API Names)

The design doc references "Qwen3.7-Max" and "Qwen3.7-Plus" — these are **not** the actual API model names. Here is the corrected mapping:

| Role in Design | Design Doc Name | **Actual API Model Name** | Notes |
|---|---|---|---|
| Orchestration / Showrunner | Qwen3.7-Max | **`qwen3.6-plus`** | 1M context, thinking mode, function calling, built-in tools. `qwen3-max` is now LEGACY. |
| High-volume agents | Qwen3.7-Plus | **`qwen3.5-plus`** | 1M context, multimodal (text + image + video), function calling. |
| High-volume agents (alt) | Qwen3.5-Plus | **`qwen3.5-plus`** | Same as above — still current, multimodal. |
| Vision: page reading + QA | Qwen3-VL | **`qwen3.6-plus`** (vision built-in!) | `qwen3-vl-plus` is now LEGACY. qwen3.6-plus and qwen3.5-plus both have vision built-in (256 images, 64 videos). |
| Flash (cheap tasks) | — | **`qwen3.6-flash`** | 1M context, same features as plus, cheaper. `qwen3.5-flash` also works. |
| Character video | Wan 2.7 | **`wan2.7-i2v`** (image-to-video) | Supports: first-frame, first-and-last-frame, video continuation. |
| Establishing video | HappyHorse 1.0 | **`happyhorse-1.0-t2v`** (text-to-video) | 720P/1080P, up to 15s. Also has i2v mode. |
| Narration | CosyVoice v3-plus | **`cosyvoice-v3-plus`** | Voice cloning + word timestamps. ⚠️ `cosyvoice-v3.5-plus` is Beijing ONLY — not available in Singapore! |
| Reference-to-video | Wan 2.7 r2v | **`wan2.7-i2v`** with ref images | Use wan2.7-i2v with reference images in the `media` array. |
| Keyframe / reference images | — | **`wan2.7-image-pro`** | Latest image generation model. Also: `qwen-image-2.0-pro`. |
| Text embeddings | — | **`text-embedding-v4`** | Up to 2048 dims, sparse vectors. For episodic store. |
| Multimodal embeddings | — | **`tongyi-embedding-vision-plus`** | Text + image + video embeddings. For CCS (character consistency). |
| Reranking | — | **`qwen3-rerank`** | For reranking retrieved shots from episodic store. |

> **See [`ALIBABA_CLOUD_MODELS.md`](./ALIBABA_CLOUD_MODELS.md) for the complete verified model catalog with all regions, capabilities, and pricing.**

### Key API Endpoints

| Purpose | Endpoint |
|---|---|
| Text generation (OpenAI-compatible) | `POST /compatible-mode/v1/chat/completions` |
| MCP tool calls (Responses API) | `POST /compatible-mode/v1/responses` |
| Video synthesis (Wan/HappyHorse) | `POST /api/v1/services/aigc/video-generation/video-synthesis` |
| Async task polling | `GET /api/v1/tasks/{task_id}` |
| TTS (CosyVoice) | `POST /api/v1/services/aigc/text-generation/generation` (speech synthesis endpoint) |

### Important API Notes

1. **Video generation is asynchronous.** Submit task → poll `GET /api/v1/tasks/{task_id}` until status is `SUCCEEDED`. Takes 1–5 minutes per clip.
2. **MCP is only via Responses API**, not Chat Completions. Use `client.responses.create()` with `tools=[mcp_tool]` where `mcp_tool` has `type: "mcp"`, `server_protocol: "sse"`, `server_url`, and `headers`.
3. **HappyHorse 1.0 is not one model but four**: t2v (text-to-video), i2v (image-to-video), r2v (reference-to-video), and video editing. The distinction matters for the Wan-mode decision tree.
4. **Wan 2.7 image-to-video** supports: first-frame-to-video, first-and-last-frame-to-video, and video continuation. All through the same endpoint with different parameters.
5. **Batch API** offers ~50% off for non-realtime work (Phase A page analysis, bulk keyframe generation).

---

## 2. System Architecture

Two planes, deliberately separated:

- **Control plane** (Scheduler) — decides *when and what* to render against the reader's attention
- **Creative/data plane** (the crew + memory + infra) — decides *how* a scene looks and produces the pixels
- **Memory store** — sits at the centre as a shared blackboard, exposed to every agent as an **MCP server**

```
┌─────────────────────────────────────────────────────────────┐
│                    FRONTEND (React)                          │
│  ┌──────────────┐  ┌──────────────────────────────────┐     │
│  │  PDF Reader   │  │  Video Stage (Viewer/Director)   │     │
│  │  (PyMuPDF)    │  │  SyncEngine · playhead · w · v   │     │
│  └──────┬───────┘  └────────────┬─────────────────────┘     │
│         │     GenerationClient (SSE/WS)  │                   │
└─────────┼───────────────────────────────┼───────────────────┘
          │ intent/seek (debounced)        │ clip_ready events
          ▼                                ▲
┌─────────────────────────────────────────────────────────────┐
│                  CONTROL PLANE                               │
│  ┌──────────────────────┐  ┌──────────────────┐             │
│  │ Scheduler/Prefetch   │←→│ Budget Service    │            │
│  │ Watermark buffer     │  │ reserve/remaining │             │
│  │ Promotion · cancel   │  └──────────────────┘             │
│  └──────────┬───────────┘                                   │
└─────────────┼───────────────────────────────────────────────┘
              │ shot spec request / enqueue render
              ▼
┌─────────────────────────────────────────────────────────────┐
│              CREATIVE PLANE — AGENT SOCIETY                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐              │
│  │Showrunner│→ │ Adapter  │→ │Continuity Sup│              │
│  │qwen3.6- │  │qwen3.5-  │  │qwen3.6-      │              │
│  │plus     │  │plus      │  │plus          │              │
│  └──────────┘  │plus      │  └──────────────┘              │
│                └──────────┘                                  │
│  ┌──────────────────┐  ┌──────────┐  ┌──────────┐          │
│  │ Cinematographer  │→ │ Generator│→ │ Critic/QA│          │
│  │ qwen3.6-plus    │  │wan2.7+   │  │qwen3.6-  │          │
│  │ (vision)        │  │cosyvoice │  │plus(vis) │          │
│  └──────────────────┘  └──────────┘  └──────────┘          │
└─────────────────────────────────────────────────────────────┘
              │ all agents read/write through:
              ▼
┌─────────────────────────────────────────────────────────────┐
│              MEMORY LAYER — MCP CANON SERVER                 │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Canon Graph │  │ Episodic/    │  │ Shot Cache   │       │
│  │ (versioned) │  │ Vector Store │  │ (hash-keyed) │       │
│  └─────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│              ALIBABA CLOUD INFRASTRUCTURE                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ DashScope/   │  │ OSS Storage  │  │ Render Queue │      │
│  │ Model Studio │  │ clips/frames │  │ + Workers    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Generation-on-Scroll

### 3.1 The Asymmetry

Generating a 5-second clip takes 30–90s wall-clock on Wan 2.7. But a reader dwells: a page of ~250 words takes 45–90 seconds to read, mapping to only ~8–15 seconds of video. The rate at which a reader *consumes* video-seconds is ~0.15–0.30 per wall-clock second. During the 60 seconds a reader spends on page N, the backend has 60 seconds to produce ~10 seconds of video for page N+1.

### 3.2 Units

- **Book** → **Scene** (1-2 pages, stitching boundary) → **Shot** (~5s clip, buffer/queue unit) → **Beat** (sentence-or-two of narrative intent, smallest planning atom)

Every shot carries a **source span** tying it to exact text:

```json
{
  "shot_id": "shot_00042",
  "beat_id": "beat_0034",
  "scene_id": "scene_005",
  "source_span": { "page": 12, "para": 3, "word_range": [4501, 4560] },
  "est_duration_s": 5.0,
  "est_cost": { "video_seconds": 5.0, "tokens": 1850 }
}
```

### 3.3 Reading-Position Model

- **Focus word index `w`** — word nearest the reading line (top third of viewport)
- **Reading velocity `v`** — EWMA of words/sec over 10s window, clamped to [0.5×, 3×] of 4 wps default
- **Direction** — forward by default; backward re-targets buffer (usually cache hit)
- **Mode** — `viewer` (video drives) or `director` (reader drives)

ETA to any future shot = `(shot.word_range.start − w) / v`

### 3.4 Three Zones

| Zone | ETA | What exists | Video budget cost |
|---|---|---|---|
| **Committed** | 0 – ~45s | Full Wan video, QA-passed, narrated, cached | **Spends video-seconds** |
| **Speculative** | ~45 – ~240s | One keyframe still per beat (image-gen) | **~zero** |
| **Cold** | > 240s | Plan + canon only | Free |

### 3.5 Watermark Buffer (Hysteresis)

- **Low watermark `L = 25s`** — when committed-seconds-ahead drops below L, start generation burst
- **High watermark `H = 75s`** — burst renders until committed-seconds-ahead reaches H, then **stops completely**
- Between L and H: **idle** — no generation

### 3.6 Velocity-Adaptive Promotion

```
for each beat B in the speculative zone, in reading order:
    eta = (B.word_range.start - w) / v
    if eta < C and trajectory_is_stable() and budget.can_afford(B.est_video_seconds):
        promote(B)            # enqueue full Wan render at committed priority
    else:
        ensure_keyframe(B)    # cheap image only; no video-seconds
```

### 3.7 Timers

- **Scroll-settle debounce (200ms)** — intent position updates only after scroll pauses
- **Dwell confirmation** — beat promoted only after `w` moves toward it for 2 consecutive settle windows
- **Idle-pause (8s)** — no scroll/playback for 8s → all speculative generation halts

### 3.8 Seek & Skip

1. **Cancel** in-flight speculative renders > 120s from new position
2. **Bridge instantly** — show keyframe under Ken-Burns pan, no spinner
3. **Re-seed** — reset focus playhead, re-run watermark fill

---

## 4. The Crew — Agents & Negotiation Protocol

### Agent Contracts

| Agent | Model | Reads | Writes |
|---|---|---|---|
| **Showrunner** | `qwen3.6-plus` | canon summary, conflict objects | scene plan, conflict resolutions |
| **Adapter** | `qwen3.5-plus` | page text + layout | beats, shot list, source spans |
| **Continuity Supervisor** | `qwen3.6-plus` | full canon + proposed shots | canon entities, continuity states, conflict flags |
| **Cinematographer** | `qwen3.6-plus` (vision built-in) | canon slice for beat | shot spec |
| **Generator** | `wan2.7-i2v` / `happyhorse-1.0-t2v` + `cosyvoice-v3-plus` | shot spec | clip, last-frame, audio, word timestamps |
| **Critic / QA** | `qwen3.6-plus` (vision built-in) | clip + canon slice | QA record (episodic) |
| **Production Manager** | `qwen3.6-flash` + `qwen3.6-plus` | pipeline state, budget, error signals | quality gate decisions, remediation actions, HITL escalations |

### Negotiation Protocol (Track 3 Money Shot)

Conflicts are **first-class structured objects**:

```json
{
  "conflict_id": "cf_001",
  "raised_by": "continuity_supervisor",
  "type": "canon_violation",
  "shot_id": "shot_00051",
  "claim": "shot depicts the heroine drawing a sword",
  "canon_fact": "state_hero_sword_001 retired at beat_0034 (sword lost in the river)",
  "current_beat": "beat_0039",
  "options": [
    { "id": "honor_canon", "action": "regenerate empty-handed", "cost_video_s": 5 },
    { "id": "surface_to_user", "action": "ask the director to choose", "cost_video_s": 0 },
    { "id": "evolve_canon", "action": "assert sword reacquired", "requires": "textual support" }
  ]
}
```

Showrunner resolution policy:
```
if option "evolve_canon" has textual support in the source span:
    -> evolve_canon
elif director is present and conflict.user_facing:
    -> surface_to_user
else:
    -> honor_canon
log(decision, reasoning) -> episodic store
```

### Per-Shot State Machine

```
Planned → Keyframed (speculative, image-gen) → Promoted (ETA < C, stable)
  → CacheCheck → Accepted (cache hit, 0 video-s) OR Rendering (cache miss)
  → QA → Accepted (all pass) OR Repair (check failed)
  → Repair → Rendering (retry ≤ 2) OR Conflict (timeline contradiction) OR Degraded (retries exhausted)
  → Conflict → Rendering (resolved) → Accepted
  → Accepted: logged to episodic, cached, last-frame → canon
  → Degraded: defect logged, Ken-Burns fallback
```

---

## 5. The Memory Layer — MCP Canon Server

### 5.1 Canon Graph (Structured)

Versioned nodes for characters, locations, props, style, and continuity states:

**Character node example:**
```json
{
  "id": "char_elsa_001",
  "type": "character",
  "name": "Elsa",
  "aliases": ["the Snow Queen"],
  "appearance": {
    "description": "young woman, platinum braid, ice-blue gown, pale skin",
    "embedding": [/* 768-d appearance vector */],
    "reference_images": [
      { "oss_url": "oss://.../char_elsa_001/ref_front.png", "pose": "front", "locked": true },
      { "oss_url": "oss://.../char_elsa_001/ref_three_quarter.png", "pose": "3q", "locked": true }
    ]
  },
  "voice": {
    "cosyvoice_voice_id": "vc_elsa_8f2a",
    "reference_audio_url": "oss://.../char_elsa_001/voice_ref.wav",
    "params": { "speed": 1.0, "pitch": 0 }
  },
  "first_appearance": { "page": 3, "beat_id": "beat_0007" },
  "version": 3,
  "valid_from_beat": "beat_0001",
  "valid_to_beat": null,
  "supersedes": "char_elsa_002"
}
```

**Continuity-state node (versioned fact):**
```json
{
  "id": "state_hero_sword_001",
  "subject": "char_hero_001",
  "predicate": "possesses",
  "object": "prop_sword_001",
  "valid_from_beat": "beat_0012",
  "valid_to_beat": "beat_0034",
  "version": 1,
  "source_span": { "page": 8, "char_range": [1203, 1280] }
}
```

### 5.2 Episodic / Vector Store

Every shot ever generated — prompt, seed, references, output URL, QA scores — embedded for retrieval.

### 5.3 MCP Tool Surface

| Tool | Signature | Purpose |
|---|---|---|
| `canon.query` | `(beat_id, kinds?) → canon_slice` | Retrieval policy — returns only what this beat needs |
| `canon.get_entity` | `(id, at_beat?) → entity` | Versioned entity resolution (time-travel reads) |
| `canon.upsert_entity` | `(entity) → version` | Continuity Supervisor writes new version |
| `canon.assert_state` | `(subject, predicate, object, valid_from) → state_id` | Add versioned fact |
| `canon.retire_state` | `(state_id, valid_to)` | **Forgetting** — close a fact's validity interval |
| `shot.plan` | `(scene_id) → shot_list` | Adapter's decomposition |
| `shot.render` | `(shot_spec) → job_id` | Enqueue render (honours cache + budget) |
| `shot.status` / `shot.result` | `(job_id)` / `(shot_id)` | Poll / fetch |
| `episodic.search` | `(embedding, filters) → shots[]` | "What worked before" — nearest prior accepted shots |
| `episodic.log` | `(shot_record)` | Write QA + outcome |
| `budget.reserve` / `budget.remaining` | `(video_seconds)` / `() → s` | Budget guardrail as a service |
| `prefs.get` / `prefs.upsert` | `(book_id?)` / `(pref)` | Director-preference read/write |

### 5.4 Caching & Dedup

```
shot_hash = sha1(book_id + beat_id + canon_version_at_render
                 + render_mode + seed + reference_set_hash)
```

Cache hit → serve from OSS → zero video-seconds. Director edits only re-render shots whose `reference_set_hash` changed.

---

## 6. The Generation Pipeline

### Phase A — Ingest (cheap, global, token-only, at import)

Runs once when a book is added; spends **zero video-seconds**.

1. **Extract** — PDF → page images + text + layout (PyMuPDF)
2. **Analyse** — `qwen3.6-plus` (vision built-in) reads text, layout, illustrations → narrative beats, entities, described visuals
3. **Populate canon** — characters, locations, props, style tokens, initial continuity states
4. **Build shot list + source-span index** — Adapter decomposes beats → shots
5. **Lock identity** — Cinematographer generates canonical keyframes (image-gen) + clone CosyVoice voice
6. **Pre-render speculative keyframes lazily** as reader approaches

### Phase B — Render a Shot (expensive, local, JIT)

Triggered by Scheduler when beat is promoted. Gated by cache + budget.

### Wan-Mode Decision Tree

```
if shot has locked character and needs motion:
    if shot must arrive on exact pose/composition:
        mode = first_and_last_frame      # wan2.7-i2v with first+last frame
    elif previous shot in same scene was accepted and is continuous:
        mode = video_continuation        # wan2.7-i2v continuation from QA-passed frame
    else:
        mode = reference_to_video        # happyhorse-1.0-r2v or wan2.7-i2v with ref images
elif shot is establishing and has no character:
    mode = text_to_video                 # happyhorse-1.0-t2v
elif minor change requested on existing accepted clip:
    mode = instruction_edit              # happyhorse video editing
```

### Narration + Sync Map

CosyVoice synthesizes narration with `word_timestamp_enabled`. Word timestamps drive:
1. Karaoke highlight (moving highlight on text layer)
2. Page-turn events
3. Audio track

```json
{
  "scene_id": "scene_005",
  "segments": [{
    "shot_id": "shot_00042",
    "video_start_s": 0.0, "video_end_s": 5.0,
    "page": 12,
    "page_turn_at_s": 4.8,
    "words": [
      { "word_index": 4501, "text": "She", "t_start": 0.10, "t_end": 0.32, "bbox": [0.12,0.34,0.04,0.02] }
    ]
  }]
}
```

### Self-Correcting Loop (Critic)

| Check | Metric | Pass condition |
|---|---|---|
| Identity | CCS — cosine sim of character crop embedding vs locked appearance embedding | ≥ **0.85** |
| Style | cosine distance from scene style centroid | ≤ **0.08** |
| Timeline | VL boolean: does any depicted fact contradict active continuity state? | must be **true** |
| Motion | artifact score (flicker/morphing/extra limbs), VL-rated 0–1 | ≤ **0.25** |

Retry cap of 2 → drop to degradation ladder (Ken-Burns over best keyframe).

---

## 7. Prompt Contracts

**Adapter (page → beats + shots):**
> "You are a screenwriter adapting a book to a shot list. Given page text and detected illustrations, output JSON: an array of beats; each beat has `summary`, `entities`, `described_visuals`, `mood`, and `source_span`. Then split beats into shots of ~5s. Output only JSON."

**Cinematographer (beat + canon slice → shot spec):**
> "Design one shot. You receive a beat and a canon slice (characters with locked reference IDs, active location, style tokens, optional previous endpoint frame, director preferences). Choose `render_mode` per the decision tree. Produce `prompt`, `negative_prompt`, `reference_image_ids`, `camera`, `seed`. Output only JSON."

**Critic (clip + canon slice → QA record):**
> "You are QA. Watch the clip and score it against the canon. Return JSON with `ccs`, `style_drift`, `timeline_ok`, `motion_artifact`, `score`, `verdict`, `reason`. Do not be charitable; a wrong face is a fail even if the scene is pretty."

**Showrunner (conflict → decision):**
> "You arbitrate production conflicts. Given a conflict object and the relevant source span, apply the resolution policy: evolve canon only with textual support; otherwise surface to the director if user-facing; otherwise honor canon. Return `{chosen_option, reasoning}`."

---

## 8. Budget Accounting

**Free tier:** ~1,650 video-seconds + ~70M tokens (90 days, Singapore).

At ~5s/shot and ~20% regeneration rate: **~260 accepted shots — roughly 22 minutes of accepted film.**

Every design decision follows: **spend tokens to save video-seconds.**

- **Budget as a service** — `budget.reserve(seconds)` before every render; `budget.remaining()` gates promotion
- **Speculation is image-only** — speculative zone never touches video budget
- **Cache dedup** — re-reads and unchanged shots after Director edit cost zero
- **Batch API (~50% off)** — for all non-realtime work
- **Budget-aware degradation** — when remaining drops below floor, ride keyframe/Ken-Burns ladder

---

## 9. Engineering — Reliability & Infrastructure

### Render Queue

- **Idempotency key** = `shot_hash` — re-enqueuing same shot is a no-op
- **Cancellation token** tied to session and trajectory
- **Exponential-backoff retries** (2s, 8s, 30s) for transient DashScope failures
- **Dead-letter path** for shots that fail repeatedly → drop to degradation

### Concurrency & Backpressure

- **Lanes:** 4 committed render slots + 2 speculative (preemptible) + keyframe/image pool
- **Backpressure:** new speculative enqueues dropped when queue saturated
- **Per-session fairness:** max concurrent render count per session

### Caching Layers

| Layer | Keyed by | Saves |
|---|---|---|
| Shot cache | `shot_hash` | full re-render (video-seconds) |
| Keyframe cache | beat + canon version | image-gen calls |
| Canon-embedding cache | entity + version | re-embedding |
| Reference-video cache | character + pose | re-locking refs |
| Request-level dedup | in-flight `shot_hash` | paying twice for same shot |

### Degradation Ladder

```
full Wan video → generated keyframe + Ken-Burns pan → book's own illustration (Ken-Burns) → plain narrated text with karaoke highlight (audio only)
```

---

## 10. Metrics & Eval Harness

| Metric | Definition | Target |
|---|---|---|
| **CCS** (Character Consistency Score) | Mean cosine sim of character crop embedding vs locked ref across all shots | Higher = better |
| **Accepted-footage efficiency** | `(1 − rejected_seconds / total_seconds) × 100` | Headline number |
| **Regeneration rate** | `regens / total_shots` | Lower = better |
| **Style drift** | Variance of style embeddings across a scene | Lower = better |
| **Latency-to-first-frame on seek** | Wall-clock from seek to coherent frame | ≈ 1 frame (keyframe bridge) |
| **Buffer health** | Fraction of reading time committed buffer stayed above L | > 99% |

**Baseline:** one `qwen3.6-plus` doing the whole pipeline with **no memory** — pure frame-chaining, no canon, no critic. Run both over the same demo book and chart CCS + efficiency side by side.

---

## 11. Deployment on Alibaba Cloud

**Required for submission** — proof that backend runs on Alibaba Cloud.

| Component | Alibaba Cloud Service |
|---|---|
| Agent services + Scheduler | ECS or Function Compute |
| Model calls | DashScope / Model Studio (Singapore endpoint) |
| Object storage (clips, frames, audio, canon vault) | OSS |
| Render queue | MNS (Message Service) or managed broker |
| Vector store | OpenSearch or self-hosted on ECS |

**Proof-of-deployment artifact** — a Python file in the repo that demonstrably uses OSS + DashScope:

```python
# deploy/alibaba_render_worker.py
import os, oss2, dashscope
from dashscope import VideoSynthesis

dashscope.api_key = os.environ["DASHSCOPE_API_KEY"]
dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

auth = oss2.Auth(os.environ["OSS_AK"], os.environ["OSS_SECRET"])
bucket = oss2.Bucket(auth, "https://oss-ap-southeast-1.aliyuncs.com", "kinora-assets")

def render_shot(spec: dict) -> dict:
    rsp = VideoSynthesis.call(
        model="wan2.7-i2v",
        prompt=spec["prompt"],
        negative_prompt=spec["negative_prompt"],
        ref_images=spec["reference_urls"],
        seed=spec["seed"],
        parameters={"duration": spec["target_duration_s"]},
    )
    clip_bytes = fetch(rsp.output.video_url)
    key = f"clips/{spec['shot_id']}.mp4"
    bucket.put_object(key, clip_bytes)
    return {"clip_url": f"oss://kinora-assets/{key}", "task_id": rsp.request_id}
```
