# DESIGN — Generator, render-provider abstraction & audio post-production

Owner domain (the only files this worktree edits):

- `backend/app/agents/generator.py`
- `backend/app/providers/video.py`, `backend/app/providers/tts.py`, and **new** audio/music
  provider modules + provider types (`backend/app/providers/audio.py`,
  `backend/app/providers/video_router.py`).
- `backend/app/render/stitch.py`, `backend/app/render/sync_map.py`, and **new** render
  modules (`audio_post.py`, `enhance.py`, `color_match.py`, `music.py`).
- New tests under `backend/tests/` for the above.

Two hard rules, always:

1. **`KINORA_LIVE_VIDEO` stays OFF.** No new code spends a Wan/DashScope credit. The
   Generator keeps *propagating* `LiveVideoDisabled`; it never fabricates a clip.
   Degradation remains the render pipeline's job (`app/render/degrade.py`, not owned here).
2. **Pure mapping/compose functions stay deterministic.** Prompt composition, provider
   routing, sync-map alignment, color-match parameter derivation, audio-mix planning and
   enhancement planning are all pure functions with no clock/RNG/network.

`§` citations are to `kinora.md`. Sections read for this work: §9.2 (Phase B render),
§9.3 (Wan-mode decision tree), §9.4 (narrate + sync map), §9.6 (stitch + ship),
§12.6 (Alibaba deployment / DashScope `VideoSynthesis`).

---

## 1. Architecture

### 1.1 Where this sits

```
Scheduler ── promotes a beat ──▶ render/pipeline.py (§9.7 state machine; NOT owned here)
                                      │  calls the ClipGenerator seam
                                      ▼
            ┌───────────────── agents/generator.py (Generator) ──────────────────┐
            │  build_wan_spec(ShotSpec) ─▶ WanSpec   (pure §9.3 mapping)          │
            │  VideoBackend.render(WanSpec) ─▶ VideoResult                         │
            │      ├─ VideoProvider (one hosted Wan id)                            │
            │      └─ VideoRouter: health-tracked ordered backends                │
            │            ├─ failover (skip OPEN, advance on retryable)            │
            │            └─ racing (first healthy success wins; opt-in)           │
            │  tts.synthesize(...) ─▶ TtsResult (+ word/phoneme timing)           │
            │  → GeneratorOutput (clip + last frame + narration + sync inputs)    │
            └─────────────────────────────────────────────────────────────────────┘
                                      │
   accepted shots in a scene ────────┘
                                      ▼
   render/color_match.py ── pure per-clip grade toward a scene reference ─┐
   render/enhance.py     ── pure EnhancePlan + real ffmpeg interpolate/upscale
   render/stitch.py      ── concat + color-match + cumulative sync merge ──▶ scene mp4 + SceneSyncMap
   render/music.py       ── deterministic mood→cue scoring (local library)
   render/audio_post.py  ── music + SFX + ducking + loudness master ───────▶ mastered scene audio
```

The Generator is the thin real bridge from a designed `ShotSpec` to pixels + narration. The
abstractions slot **inside** it (the video router) and **after** stitch (the audio post
pipeline), so the `ClipGenerator` / `SceneStitcher` contracts the rest of the backend
depends on do not change shape.

### 1.2 Multi-provider video interface (`VideoBackend` + `VideoRouter`)

- **`VideoBackend` (Protocol):** `name`, `async render(WanSpec) -> VideoResult`,
  `async healthy() -> bool`. `VideoProvider` satisfies it (gains `name`/`healthy()`).
- **`BackendHealth`:** per-backend circuit-breaker-style record (consecutive failures →
  cooldown → half-open probe). Pure-logic, driven by an injectable clock.
- **`VideoRouter`:** ordered list of `(backend, weight)`; honours the gate.
  - **Failover (default):** try in priority order, skip `OPEN`; advance on *retryable*
    `ProviderError`; a non-retryable error or `LiveVideoDisabled` short-circuits.
  - **Racing (opt-in):** start top *k* healthy backends; first success wins, rest cancel.
  - `LiveVideoDisabled` is *propagated unchanged* and never counts as a health failure.

### 1.3 Frame-interpolation & upscaling hooks (`render/enhance.py`)

A post-render *enhancement* stage smooths/upscales a cheap clip with ffmpeg
(`minterpolate` fps interpolation, `scale`+`unsharp` upscale) with no model call. Modeled
as a pure `EnhancePlan` (target fps, size, sharpen) derived deterministically from the
clip's probe + a profile, plus a real ffmpeg executor. No model spend.

### 1.4 Audio post-production (`render/audio_post.py` + `render/music.py`)

Narration → a **mixed, mastered** scene track, all real ffmpeg, deterministic, zero spend:

1. **Music scoring:** mood/palette → royalty-free cue from a local library (deterministic
   table in `music.py`); looped/trimmed to scene length.
2. **SFX placement:** `(t, gain_db)` events laid over the bed with `adelay`.
3. **Dialogue ducking:** `sidechaincompress` the bed against narration so it drops under
   speech and swells in the gaps.
4. **Loudness mastering:** final `loudnorm` (EBU R128) to a target LUFS so every scene
   plays at a consistent level.

Pure planners (`plan_mix`, `ducking_filter`, `loudnorm_filter`, `score_scene`) are tested
without ffmpeg; full `master_scene_audio` against real ffmpeg (skip when no binary).

### 1.5 Shot-to-shot color matching (`render/color_match.py`, applied in stitch)

Pure planner: from each clip's `signalstats` (mean luma + per-channel mean) derive a gentle
`eq`/`colorbalance` correction toward the **scene reference** (the first/full-quality clip),
clamped so it never over-corrects. Statistic extraction uses ffmpeg; the correction
derivation is pure. Applied in `_normalize_segment` before concat.

### 1.6 Richer sync map (word + phoneme timing)

Adds **phoneme** sub-timing inside each word so karaoke can animate *within* a long word
and future viseme work has anchors. Phonemes come from a pure grapheme→chunk splitter that
distributes each word's `[t_start, t_end]` across chunks by length — anchored to real word
timing, never inventing duration. `SyncWord` gains optional `phonemes: list[SyncPhoneme]`.

---

## 2. Contracts (types crossing module / domain boundaries)

### 2.1 Owned new types (additive)

- `providers/video_router.py`: `VideoBackend` Protocol, `BackendStatus`, `BackendHealth`,
  `RouterPolicy`, `VideoRouter`. Only `VideoResult` (unchanged) crosses out.
- `providers/audio.py`: `MusicCue`, `SfxEvent`, `MixProfile`, `MixPlan`, `MusicProvider`
  Protocol, `LocalCueLibrary`.
- `render/music.py`: `Mood`, `score_scene`, the deterministic mood→cue table.
- `render/sync_map.py`: `SyncPhoneme`; `SyncWord.phonemes = []` (new optional field).
- `render/audio_post.py`: `SceneAudioResult`.
- `render/enhance.py`: `EnhanceProfile`, `EnhancePlan`, `SceneEnhanceResult`.

### 2.2 Seams consumed (NOT owned — read-only)

- `render/degrade.py`: `get_ffmpeg_exe`, `run_ffmpeg`, `inspect`, `FfmpegError`,
  `FILM_SIZE`, `DEFAULT_FPS`, `ProbeInfo`. Reused, not modified.
- `providers/base.py`: `ProviderClient`, `data_uri`, `sdk_get`. Reused.
- `providers/errors.py`: `LiveVideoDisabled`, `ProviderError`, `TransientProviderError`.
- `agents/contracts.py`: `ShotSpec`, `Camera`, `RenderMode`. Read-only.

### 2.3 `ClipGenerator` seam (unchanged shape)

`render/pipeline.py:ClipGenerator` and `Generator.render(...)` are **unchanged** — the
router lives behind `self._providers.video`.

---

## 3. Integration

- **Generator → backend:** `Generator` accepts any `VideoBackend` (the existing
  `VideoProvider` or a `VideoRouter`). `create_providers` keeps returning a single
  `VideoProvider` by default; `create_video_router(...)` builds the multi-backend router.
- **Stitch → color match + audio:** `concat_clips` gains optional `color_match=False`;
  `SceneStitcher` gains optional flags (default = current behaviour).
- **Sync map → client:** `phonemes` rides through `merge_sync_segments` (timings shift with
  the word).

---

## 4. Phased roadmap (living: done vs. remaining)

| Phase | Scope | Status |
|---|---|---|
| **1** | Multi-provider `VideoRouter` (failover + racing) + per-backend health; `VideoProvider` becomes a `VideoBackend`; `Generator` takes a `VideoBackend`. | **DONE** |
| **2** | Audio post: `music.py` (mood→cue scoring) + `audio.py` (mix types/planner) + `audio_post.py` (ducking/loudnorm pure + real-ffmpeg master). | **DONE** |
| **3** | `enhance.py` frame-interpolation + upscale (pure plan + real ffmpeg). | **DONE** |
| **4** | `color_match.py` shot-to-shot grade + applied in `stitch.concat_clips`. | **DONE** |
| **5** | Phoneme sub-timing in `sync_map.py` (`SyncPhoneme`, `split_phonemes`, wired into builder + merge). | **DONE** |
| **6** | Racing-by-budget policy; weighted backend selection; cost-aware `RouterPolicy`. | **DONE** |
| **7** | `tts.py`/`prosody.py` richer prosody planning (per-word emphasis from punctuation, SSML-style break planning, instruct style string) — pure, no new spend. | **DONE** |
| **8** | Audio mastering profiles (cinematic / dialogue-forward / quiet-room / punchy) + per-mood preset recommendation + per-mood music intensity; SFX library taxonomy; `score_scene_to_audio` one-call entry. | **DONE** |
| **13** | Viseme track (`visemes.py`) — deterministic grapheme→viseme mapping from the phoneme map, per-word frames + segment track with rest fills + coalescing (lip-flap / accessibility anchor). | **DONE** |
| **14** | Caption export (`captions.py`) — sync map → WebVTT/SRT cues with readable packing (char/word/duration limits + sentence-end breaks); accessibility (§3). | **DONE** |
| **16** | Narration↔clip retiming (`sync_map.rescale_word_timings`) — linearly rescale word timings so karaoke stays locked when TTS length ≠ clip length (§9.4). | **DONE** |
| 9 | Wire router into `create_providers` behind a `video_backends` setting; add §12.6 Alibaba `VideoSynthesis` backend. | seams ready (additive config — not owned) |
| 10 | Hosted music-gen `MusicProvider` replacing the local cue library; hosted instruct-TTS path consuming `ProsodyPlan.style_instruction`. | types ready |
| 11 | Learned per-reader audio profile folded into `MixProfile` (mirrors §8.6 learned-camera prior); `intensity_override` already plumbed. | profile shape ready |
| 12 | GPU frame-interp (RIFE/FILM) + ML upscale behind the `EnhancePlan` executor. | hook ready |
| 15 | Wire viseme/caption tracks into the §9.6 stitch artifact + the renderer API client (`apps/desktop/src/lib/api.ts`, not owned — additive client field). | types ready |

### Known pre-existing baseline issue (NOT introduced here)

`backend/tests/test_providers_openai_chat.py:45` (`_openai_client`) is missing a
return annotation → one `mypy` error in `make lint`. That file is in the
OpenAI/reasoning-provider domain (a sibling agent's), is **unmodified by this
worktree**, and the error exists in the committed baseline (commit `0aa49c4`). Left
untouched to respect domain boundaries; flagged here so it is not attributed to this
work. Every file this worktree owns is `mypy`- and `ruff`-clean.

---

## 5. Cross-domain contract changes (for sibling agents)

**No breaking changes — everything additive:**

1. `render/sync_map.py::SyncWord` gains optional `phonemes: list[SyncPhoneme] = []`; new
   class `SyncPhoneme`. `extra="forbid"` models that round-trip a `SyncWord` without
   `phonemes` still validate (default empty).
2. `render/stitch.py::concat_clips` gains optional `color_match=False`; `SceneStitcher`
   gains optional `color_match`/`audio_master` flags. Existing calls unchanged.
3. New files `providers/audio.py`, `providers/video_router.py`, `render/music.py`,
   `render/audio_post.py`, `render/enhance.py`, `render/color_match.py` — no sibling
   imports them.
4. `agents/generator.py::Generator.__init__` accepts a `VideoBackend`-typed video provider;
   `Providers.video` already satisfies it. `Generator(providers)` still works unchanged.

If a sibling needs the router/audio master wired into `composition.py`/`config.py`, that is
an additive Phase 9 change (not owned here) — flagged so it is expected.

---

## 6. Module map (what exists after this run)

| File | Kind | Purpose |
|---|---|---|
| `providers/video_router.py` | new | `VideoBackend`, `BackendHealth`, `BackendTier`, `VideoRouter` (failover/race/cost-aware), `order_for_budget` |
| `providers/audio.py` | new | mix types, `plan_mix`, `LocalCueLibrary`, `MusicProvider`, `MasterPreset`/`MixProfile`, `recommend_profile`, SFX taxonomy |
| `providers/prosody.py` | new | `ProsodyPlan`, `plan_prosody` (emphasis + break planning + instruct style string) |
| `render/music.py` | new | mood taxonomy + deterministic cue scoring (`score_scene`) |
| `render/audio_post.py` | new | ducking/loudnorm pure + `master_scene_audio` + `score_scene_to_audio` (real ffmpeg) |
| `render/enhance.py` | new | `EnhancePlan` + `plan_enhancement` + interpolate/upscale ffmpeg |
| `render/color_match.py` | new | per-clip grade toward scene reference (`derive_correction`, `measure_stats`) |
| `render/visemes.py` | new | grapheme→viseme map + `segment_visemes` (lip-flap / accessibility anchor) |
| `render/captions.py` | new | sync map → WebVTT/SRT cue export |
| `render/sync_map.py` | +phoneme/retime | `SyncPhoneme`, `split_phonemes`, `grapheme_chunks`, `rescale_word_timings`, wired into builder |
| `render/stitch.py` | +color | `color_match` flag in `concat_clips`/`SceneStitcher`; phoneme-aware merge |
| `providers/video.py` | +backend | `name`/`healthy()` → `VideoBackend` |
| `providers/tts.py` | +prosody | `plan_prosody` accessor |
| `agents/generator.py` | +backend | takes a `VideoBackend` (default = providers.video) |
| `providers/__init__.py` | +exports | `create_video_router`, router/tier/audio symbols |
