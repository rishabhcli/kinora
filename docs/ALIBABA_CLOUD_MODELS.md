# Alibaba Cloud / DashScope — Complete Model Catalog (Verified June 2026)

> All model names verified against official Alibaba Cloud Model Studio documentation. **Only models available in the Singapore (International) region are listed as available** — that's the hackathon endpoint.

---

## Critical Finding: Model Names Have Changed

The original `kinora.md` design doc used model names like "Qwen3.7-Max", "Qwen3-VL", etc. **These are wrong.** The actual API model names are different, and the lineup has been updated:

| Design Doc (WRONG) | Actual API Model Name | Status |
|---|---|---|
| Qwen3.7-Max | `qwen3.6-plus` or `qwen3.6-max-preview` | ✅ Current |
| Qwen3.7-Plus | `qwen3.6-plus` | ✅ Current |
| Qwen3.5-Plus | `qwen3.5-plus` | ✅ Still available |
| Qwen3-VL | `qwen3.6-plus` (vision now built-in!) | ✅ Legacy `qwen3-vl-plus` works but deprecated |
| Qwen3-VL-Plus | `qwen3.6-plus` or `qwen3.5-plus` (both have vision) | ✅ Vision is now in main models |
| CosyVoice v3-plus | `cosyvoice-v3-plus` | ✅ Correct for Singapore |
| CosyVoice v3.5-plus | `cosyvoice-v3.5-plus` | ❌ **Beijing only — NOT available in Singapore!** |
| Wan 2.7 | `wan2.7-i2v` / `wan2.7-t2v` | ✅ Current |
| HappyHorse 1.0 | `happyhorse-1.0-t2v` / `happyhorse-1.0-i2v` | ✅ Current |

---

## Text Generation Models (Singapore Region)

### Current — Qwen3.6 Series (Recommended)

| Model ID | Context | Max Output | Thinking | Function Calling | Built-in Tools | Structured Output | Batch |
|---|---|---|---|---|---|---|---|
| `qwen3.6-max-preview` | 256k | 64k | ✅ | ✅ | ❌ | ✅ | ❌ |
| `qwen3.6-plus` | **1M** | 64k | ✅ | ✅ | ✅ | ✅ | ✅ |
| `qwen3.6-flash` | **1M** | 64k | ✅ | ✅ | ✅ | ✅ | ✅ |

### Current — Qwen3.5 Series (Still Available)

| Model ID | Context | Max Output | Thinking | Function Calling | Built-in Tools | Structured Output | Batch |
|---|---|---|---|---|---|---|---|
| `qwen3.5-plus` | **1M** | 64k | ✅ | ✅ | ✅ | ✅ | ✅ |
| `qwen3.5-flash` | **1M** | 64k | ✅ | ✅ | ✅ | ✅ | ❌ |

### Legacy (No Longer Recommended — But Still Work)

| Model ID | Context | Notes |
|---|---|---|
| `qwen3-max` | 131k | Replaced by `qwen3.6-max-preview` |
| `qwen-plus` | 131k | Alias, maps to `qwen3.5-plus` in some regions |
| `qwen3-vl-plus` | 128k | **Replaced by `qwen3.6-plus` which has vision built-in** |
| `qwen3-vl-flash` | 128k | Replaced by `qwen3.6-flash` |

### US Region Variants

| Model ID | Context | Notes |
|---|---|---|
| `qwen-plus-us` | 1M | US Virginia endpoint |
| `qwen-flash-us` | 1M | US Virginia endpoint |

---

## Vision / Multimodal Models (Singapore Region)

### ⚠️ Major Change: qwen3.6 and qwen3.5 series now have VISION BUILT-IN

You no longer need a separate VL model. `qwen3.6-plus`, `qwen3.6-flash`, `qwen3.5-plus`, and `qwen3.5-flash` all accept text, images, AND video as input.

| Model ID | Input Modalities | Context | Max Images | Max Videos | Function Calling | Built-in Tools |
|---|---|---|---|---|---|---|
| `qwen3.6-plus` | Text, Images, Video | **1M** | 256 | 64 | ✅ | ✅ |
| `qwen3.6-flash` | Text, Images, Video | **1M** | 256 | 64 | ✅ | ✅ |
| `qwen3.5-plus` | Text, Images, Video | **1M** | 256 | 64 | ✅ | ✅ |
| `qwen3.5-flash` | Text, Images, Video | **1M** | 256 | 64 | ✅ | ✅ |

### Omni Models (Real-time multimodal — text + audio + vision)

| Model ID | Input | Output | Context | Notes |
|---|---|---|---|---|
| `qwen3.5-omni-plus` | Text, Audio, Image, Video | Text, Audio | 1M | Real-time multimodal |
| `qwen3.5-omni-flash` | Text, Audio, Image, Video | Text, Audio | 1M | Cheaper omni |

### Legacy Vision Models (Deprecated — Don't Use for New Projects)

| Model ID | Notes |
|---|---|
| `qwen3-vl-plus` | Replaced by `qwen3.6-plus` |
| `qwen3-vl-flash` | Replaced by `qwen3.6-flash` |
| `qwen-vl-ocr` | Still available for OCR-specific tasks |
| `qvq-max` | Visual reasoning model |

---

## Video Generation Models (Singapore Region)

### Wan 2.7 Series (Current — Recommended)

| Model ID | Task | Key Features | Duration | Resolution |
|---|---|---|---|---|
| `wan2.7-i2v` | Image-to-Video | First-frame, first-and-last-frame, video continuation | 2-15s | 720P, 1080P |
| `wan2.7-t2v` | Text-to-Video | Text prompt → video | 2-15s | 720P, 1080P |

**API endpoint (Singapore):**
```
POST https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
```
New workspace domain: `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api/v1`

**API is asynchronous:** Create task → poll for result with `task_id` (valid 24 hours).

### Wan 2.6 Series (Legacy)

| Model ID | Task | Notes |
|---|---|---|
| `wan2.6-i2v` | Image-to-Video | First-frame only |
| `wan2.6-i2v-flash` | Image-to-Video | Faster, cheaper |
| `wan2.6-t2v` | Text-to-Video | Older T2V |

### HappyHorse 1.0

| Model ID | Task | Notes |
|---|---|---|
| `happyhorse-1.0-t2v` | Text-to-Video | Alternative to Wan |
| `happyhorse-1.0-i2v` | Image-to-Video | Alternative to Wan |

### Older Legacy (Don't Use)

`wanx2.1-t2v-turbo`, `wanx2.1-t2v-plus`, `wanx2.1-i2v-turbo`, `wanx2.1-i2v-plus`, `wanx2.1-kf2v-plus`

---

## Image Generation Models (Singapore Region)

| Model ID | Task | Key Features |
|---|---|---|
| `wan2.7-image-pro` | Text-to-Image / Image-to-Image | Latest, highest quality |
| `qwen-image-2.0-pro` | Text-to-Image / Image Editing | Qwen's image model, strong text rendering |
| `wan2.6-t2i` | Text-to-Image | Older, still available |
| `wan2.6-image` | Image-to-Image | Older, still available |

**Use case in Kinora:** Character reference images, keyframe generation, style references.

---

## Speech Synthesis / TTS (Singapore Region)

### ⚠️ CRITICAL: cosyvoice-v3.5-plus is Beijing ONLY!

| Model ID | Region | Voice Cloning | Voice Design | Timestamps | Notes |
|---|---|---|---|---|---|
| `cosyvoice-v3-plus` | ✅ Singapore | ✅ | ❌ | ✅ (manual enable) | **Use this for hackathon** |
| `cosyvoice-v3-flash` | ✅ Singapore | ✅ | ❌ | ✅ (manual enable) | Cheaper, faster |
| `cosyvoice-v3.5-plus` | ❌ Beijing only | ✅ | ✅ | ❌ | NOT available in Singapore |
| `cosyvoice-v3.5-flash` | ❌ Beijing only | ✅ | ✅ | ❌ | NOT available in Singapore |

**For the hackathon (Singapore endpoint):** Use `cosyvoice-v3-plus` for narration with voice cloning and word timestamps.

**Timestamp feature:** Must be manually enabled. Supported by `cosyvoice-v3-plus` and `cosyvoice-v3-flash`. This is critical for Kinora's karaoke word highlighting.

**Voice cloning:** Requires 10-20 seconds of audio sample. No traditional training needed.

**API:** WebSocket-based for streaming, or HTTP for batch synthesis.
- Singapore WebSocket: `wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference`

---

## Speech Recognition / ASR (Singapore Region)

| Model ID | Task | Notes |
|---|---|---|
| `qwen3-asr-flash` | Speech-to-Text | Fast, multilingual |
| `paraformer-v1` | Speech-to-Text | Legacy |
| `fun-asr-realtime` | Real-time ASR | Streaming |

**Use case in Kinora:** Not directly needed (we generate narration from text), but could be used for verifying audio-text alignment.

---

## Text Embedding Models (Singapore Region)

| Model ID | Max Input | Dimensions | Sparse Vectors | Notes |
|---|---|---|---|---|
| `text-embedding-v4` | 8,192 tokens | 64, 128, 256, 512, 768, 1024, 1536, 2048 | ✅ | **Recommended** — Qwen3-Embedding series |
| `text-embedding-v3` | 8,192 tokens | 64, 128, 256, 512, 768, 1024 | ✅ | Older |

**OpenAI-compatible endpoint (Singapore):**
```
POST https://dashscope-intl.aliyuncs.com/compatible-mode/v1/embeddings
```

**Use case in Kinora:** Episodic vector store (FAISS) — embedding shot records, scene descriptions, and retrieval for "what worked before."

---

## Multimodal Embedding Models (Singapore Region)

| Model ID | Modalities | Dimensions | Notes |
|---|---|---|---|
| `tongyi-embedding-vision-plus` | Text, Image, Video, Multi-images | 64-1152 (default 1152) | **Use for CCS** — character consistency |
| `tongyi-embedding-vision-flash` | Text, Image, Video, Multi-images | 64-768 (default 768) | Cheaper alternative |

**Use case in Kinora:** Character Consistency Score (CCS) — embed character reference images + generated frames, compute cosine similarity. Also for style drift detection.

---

## Reranking Models (Singapore Region)

| Model ID | Task | Notes |
|---|---|---|
| `qwen3-rerank` | Document relevance scoring | Latest |
| `gte-rerank-v2` | Document relevance scoring | Older |

**Use case in Kinora:** Reranking retrieved shots from episodic store for better "what worked before" retrieval.

---

## API Endpoints Summary (Singapore Region)

| API Type | Endpoint |
|---|---|
| **OpenAI-compatible Chat** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| **OpenAI-compatible Embeddings** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/embeddings` |
| **DashScope Native (Text)** | `https://dashscope-intl.aliyuncs.com/api/v1` |
| **Video Generation** | `https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis` |
| **Image Generation** | `https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation` |
| **TTS (WebSocket)** | `wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` |
| **New workspace domain** | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api/v1` |

**US Virginia endpoints:** Replace `dashscope-intl` with `dashscope-us`

---

## Kinora's Updated Model Stack

| Agent / Component | Model | Purpose | Why |
|---|---|---|---|
| **Showrunner** | `qwen3.6-plus` | Planning, conflict arbitration | 1M context, thinking mode, function calling, built-in tools |
| **Adapter** | `qwen3.5-plus` | PDF → screenplay → shot list | 1M context, cheaper than 3.6, still powerful |
| **Continuity Supervisor** | `qwen3.6-plus` | Canon writes, inconsistency detection | Needs thinking mode for complex reasoning |
| **Cinematographer** | `qwen3.6-plus` | Shot design (needs vision for reference images) | Vision built-in, 1M context, function calling |
| **Generator (video)** | `wan2.7-i2v` | Image-to-video (primary) | First-frame, first-and-last-frame, continuation |
| **Generator (fallback video)** | `wan2.7-t2v` | Text-to-video (establishing shots) | When no reference image available |
| **Generator (alt video)** | `happyhorse-1.0-i2v` | Alternative I2V | Different style, fallback |
| **Generator (narration)** | `cosyvoice-v3-plus` | TTS + voice cloning + word timestamps | **Only TTS with timestamps in Singapore** |
| **Critic / QA** | `qwen3.6-plus` | Visual QA scoring (needs vision) | Vision built-in, 1M context, can analyze video |
| **Production Manager** | `qwen3.6-flash` | Routing, classification, quality gates | Cheap, fast, 1M context, function calling |
| **Production Manager (complex)** | `qwen3.6-plus` | Budget strategy, content safety decisions | When flash isn't enough |
| **Keyframe generation** | `wan2.7-image-pro` | Character reference images, keyframes | Latest image gen, highest quality |
| **Keyframe (alt)** | `qwen-image-2.0-pro` | Alternative image gen | Strong text rendering in images |
| **Episodic store embeddings** | `text-embedding-v4` | Shot record embeddings for retrieval | Best text embedding, up to 2048 dims |
| **CCS embeddings** | `tongyi-embedding-vision-plus` | Character consistency (image embeddings) | Multimodal: text + image + video |
| **Reranking** | `qwen3-rerank` | Rerank retrieved shots | Better retrieval quality |
| **OCR (PDF extraction)** | `qwen3.6-plus` | PDF page analysis (vision built-in) | Can read text from images/pages directly |
| **Content safety scan** | `qwen3.6-flash` | Pre-scan PDF for risky content | Fast, cheap classification |

---

## MCP Integration

MCP (Model Context Protocol) is supported via the **Responses API** (`client.responses.create`), not the standard Chat Completions API.

- MCP servers must use **SSE (Server-Sent Events)** protocol
- Register MCP tools in the `tools` parameter: `type: "mcp"`, `server_protocol: "sse"`, `server_url`, `headers`
- Maximum 10 MCP servers per request
- The Responses API also supports built-in tools: web search, code interpreter, web extractor

**Available on:** `qwen3.6-plus`, `qwen3.6-flash`, `qwen3.5-plus`, `qwen3.5-flash`

---

## Batch API

For Phase A analysis (processing the entire PDF at ingest time), use the Batch API to reduce costs:

- Supported on: `qwen3.6-plus`, `qwen3.6-flash`, `qwen3.5-plus`
- Submit batch jobs via the DashScope API
- Results delivered asynchronously, lower cost per token

---

## Pricing Notes (Free Tier)

- **$40 Qwen Cloud voucher** for hackathon participants
- **~1,650 video-seconds** free tier (Wan/HappyHorse)
- **~70M tokens** free tier (text models)
- **90 days** validity after activation
- **text-embedding-v4:** $0.07 per 1M tokens
- **cosyvoice-v3-plus:** Higher cost (~$0.286706 per 10K characters for some models)
- **Video generation:** Most expensive — budget carefully

---

## SDK

```bash
pip install dashscope    # Native DashScope SDK
pip install openai       # OpenAI-compatible (use with base_url)
```

**DashScope SDK:**
```python
import dashscope
dashscope.base_http_api_url = 'https://dashscope-intl.aliyuncs.com/api/v1'
```

**OpenAI SDK (compatible mode):**
```python
from openai import OpenAI
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
```
