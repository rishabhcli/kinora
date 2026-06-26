# Backend AI token / cost efficiency — implementation notes (P3)

> Status: **documented, not yet applied in code.** These levers touch the
> contract-bound six-agent pipeline and the DashScope provider seam. Their
> correctness depends on model determinism + live behaviour that the unit suite
> (which skips infra) can't confirm, so they were deliberately **not** changed
> blindly. Apply against the live stack (`make stack-up`) and verify per-lever.
>
> Context: video-seconds are already hard-capped (budget + `KINORA_LIVE_VIDEO`
> gate + shot-hash cache + Ken-Burns degradation). The levers below cut **LLM
> token** spend, which is dominated by Phase-A ingest (batched, ~50% off) plus
> per-shot Cinematographer/Critic calls.

## Levers (highest ROI first)

1. **Context-cache the static prompt + canon preamble (DashScope context cache).**
   System prompts are `VersionedPrompt` structs (`backend/app/agents/prompts.py`)
   and every call funnels through `BaseAgent.run_json` → `providers.chat.chat_json`
   (`backend/app/agents/base.py:79,95`). Put the stable prefix (system prompt +
   invariant canon/style preamble) first and mark it cacheable so repeated
   per-shot calls pay ~10% for the cached prefix.
   - **Safety:** transparent (same output). **Verify:** token deltas via the
     `Usage` dataclass logs (`backend/app/providers/types.py:38`).

2. **Memoize the deterministic Cinematographer fill.**
   `decide_render_mode` is already a pure function; the model fill
   (`backend/app/agents/cinematographer.py`) is a function of
   `(beat_id, canon_version, preferences_hash, render_mode, seed)`. Cache the
   fill on that key — a re-read of the same beat then costs zero tokens.
   - **Safety:** only sound if the fill is deterministic for a fixed key
     (confirm the seed is part of the key, not regenerated). **Verify:** unit
     test that two identical inputs hit the cache; live smoke that a Director
     edit (canon_version bump) correctly misses.

3. **In-flight de-duplication of agent calls.**
   The render queue's idempotency key is `shot_hash` only
   (`backend/app/queue/redis_queue.py`), so a beat re-enqueued while its
   Cinematographer call is in flight can issue a duplicate. Key in-flight calls
   by `(beat_id, canon_version)` and await the pending promise.
   - **Safety:** behaviour-preserving (same result, fewer calls). **Verify:**
     concurrency test issuing two simultaneous requests for one beat.

4. **Model tiering for text-only per-shot calls.**
   Per-shot Cinematographer input is pure text (beat summary + canon slice);
   page-image analysis (multimodal) only happens in Phase A. Route the per-shot
   text calls to a cheaper Qwen tier and reserve `qwen-vl-max` / `qwen3.7-max`
   for image analysis + conflict arbitration (`backend/app/core/config.py:42`).
   - **Safety:** quality risk — A/B the cheaper tier on the Critic pass-rate
     before committing.

5. **Velocity-aware idle-pause + episodic retirement.**
   `IDLE_PAUSE_MS` (`backend/app/scheduler/service.py:42`) and canon
   `retire_state` (`backend/app/memory/canon_service.py`) already trim
   speculative spend; tune the idle window per reading-velocity percentile and
   retire states older than ~20 scenes to keep the retrieved context flat.
   - **Safety:** tuning only. **Verify:** buffer-ahead + token logs under fast
     vs slow reading.

## Verification harness
- `make test` (unit suite, no infra) for the pure-function + cache-hit tests.
- `make stack-up` + a seeded demo book + `KINORA_LIVE_TESTS=1` for the live
  smokes; compare `Usage` token totals before/after per lever.
