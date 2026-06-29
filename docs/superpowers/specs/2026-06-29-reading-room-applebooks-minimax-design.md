# Reading-room redesign (Apple Books-style pop-out) + scrolling overhaul + MiniMax video — design

Date: 2026-06-29
Status: approved (design), pending spec review
Scope owner: desktop reading room (`apps/desktop`) + backend video provider (`backend/`)

## 1. Goal

Turn the Kinora reading room into a clean, Apple Books-inspired experience and add a
cheap hosted video provider so a real test film can be generated within a hard $30 cap.

Three deliverables, in this order (scrolling must land before the test video, per the user):

1. **Scrolling overhaul** — buttery, native continuous scroll.
2. **Reading-room redesign** — book opens as its own floating window; the old top bar is
   replaced by an Apple Books-style minimal toolbar; full-bleed film on the **left**, clean
   reading column on the **right**, never overlapping.
3. **MiniMax (Hailuo) video backend** — cheapest model, hard $30 ceiling — then generate one
   small gated test video.

## 2. User-confirmed decisions

- Reading mechanic: **continuous scroll, overhauled** (not pagination).
- Film placement: **full-bleed cinematic film in its own region**; **text is never rendered over
  the video**. Film on the **left**, reading column on the **right**, hairline divider, draggable.
- The book **pops out into its own window** in front of the library, which stays open behind.
- MiniMax: use the **cheapest published model** (`MiniMax-Hailuo-2.3-Fast` @ 768P/6s, $0.19/clip).
  Do **not** chase the unverified 512P/$0.08 path for now.
- `MINIMAX_API_KEY` is already written to `backend/.env` (gitignored).

## 3. Current state (verified)

### 3.1 Reading room (`apps/desktop/src/reading/`)
- `ReadingRoomShell.tsx` — the **top bar to remove** lives at lines ~209–404: Back, title/author,
  AI Film toggle (`onToggleGenerate`, persisted `kinora.reading.generateVideo`), bookmark
  (`kinora.bookmark.{bookId}`), Highlight mode (`kinora.highlights.{bookId}`), settings gear
  (opens `ReadingControls`), and the "Buffered Ns ahead" pill (`session.bufferAhead`).
- `ScrollFilmEngine.tsx` — two-column layout: scrolling text (left) + pinned 9:16 film (right),
  draggable splitter, `filmWidth` state (200–560px). Paragraphs `<p data-para>`; active paragraph
  brightened, others dimmed to 62% via `paintParagraph()` on rAF.
- `useScrollFilm.ts` — native `scroll` listener + rAF loop; computes scroll `fraction` →
  focus word → segment/local fraction → film `currentTime`; EMA velocity drives scrub-vs-play
  (`IDLE_MS = 220`). Paragraph tops measured once, re-measured by ResizeObserver (resize mid-scroll
  can misalign until the next callback).
- `FilmPane.tsx` — video surface; scrub seeks throttled ~30Hz (`SCRUB_SEEK_INTERVAL_MS = 34`),
  0.5s crossfade in play mode.
- `ReadingControls.tsx` + `a11y/readingPrefs.ts` — themes (Dark/Night/Sepia/Paper), font family,
  scale, leading, measure, spacing, brightness, TTS; persisted globally (`kinora.readingPrefs`),
  applied via `html[data-theme]`. A `readingMode` setting ("scroll" | "paged") exists but only
  "scroll" is implemented.
- Open flow: `HomePage.handleOpen` → `BookOpenTransition` (shared-element morph) → `ReadingRoomShell`
  mounted as a **full-screen in-app overlay** in the same window.

### 3.2 Window manager (`apps/desktop/electron/`)
- `window-manager.ts`: `createPrimary()` builds the single main window; `createWindow(route?)`
  exists, supports hash routing (`#/...`), but is **not** used for books today.
- No `openBook`/multi-window IPC for the reading flow.

### 3.3 Backend video (`backend/app/`)
- `providers/video_router.py` — `VideoBackend` protocol: `name: str`,
  `async render(spec: WanSpec) -> VideoResult`, `async healthy() -> bool`. Optional `VideoRouter`
  (failover / race / cost-aware) exists but fans over Wan model ids only.
- `providers/video.py` — `VideoProvider` (DashScope/Wan): `_model_for(spec)` picks model by mode;
  `_submit` → `POST {native_base}/{_VIDEO_PATH}` with `X-DashScope-Async: enable`;
  `_poll_to_completion` (GET tasks/{id}, backoff 1.5×); `download(url)`; raises `LiveVideoDisabled`
  when `kinora_live_video` is off; records `Usage(video_seconds=duration)`.
- `providers/base.py` — `ProviderClient.request_json(...)` (resilient: retries, breaker,
  rate-limit), `_auth_headers` (`Authorization: Bearer <key>`), `download(url)`.
- `providers/__init__.py` — `create_providers()` builds `video = VideoProvider(client)` at L147
  (no branching). `composition.py` L313 calls it with no backend arg.
- `agents/generator.py` — `ClipGenerator.__init__(providers, *, video_backend=None)`;
  `self._video = video_backend or providers.video` (L189/193) — a clean injection seam.
- `core/config.py` — video settings L82–91 (`video_model` etc., all Wan), `kinora_live_video`
  (L193, default False), budget L172–175 (`budget_ceiling_video_s=1650`, `budget_per_session_s=300`,
  `budget_per_scene_s=90`, `budget_low_floor_s=120`). **No `video_backend` setting exists.**
- `memory/budget_service.py` — `reserve(video_seconds, ...)` (checks global/session/scene caps,
  raises `BudgetExceeded`), `commit(reservation, actual_seconds)`. Currency is **video-seconds**.
- `render/pipeline.py` — `_render_live_loop`: reserve → `generator.render(spec)` → QA → commit;
  gate at L628–632 (`can_render_live()` / `is_low()` → degrade to Ken-Burns).
- No existing `minimax`/`hailuo` references anywhere (greenfield).

## 4. Design

### A. Pop-out book window
- New IPC `openBook(bookId)` (preload `window.kinora.openBook`) → `windows.createWindow('#/book/' + id)`.
- Book window uses `titleBarStyle: 'hiddenInset'` (frameless with native traffic lights overlaid on
  the toolbar) to match the Apple Books frame; keep cross-platform fallbacks consistent with the
  existing window config (vibrancy on macOS, acrylic on Win11).
- Renderer routes on `#/book/:id`: when present, mount only the reading room (no library chrome);
  otherwise render the library/home as today.
- Main window stays open behind, **lightly dimmed** while a book window is open (focus cue); undim on
  close/blur. Closing the book window returns focus to the library.
- The library click path switches from the in-app `BookOpenTransition` overlay to calling `openBook`.
  Keep `BookOpenTransition` in the tree only if still used elsewhere; otherwise retire it from the
  open path (do not delete unrelated usages).
- Multiple book windows are allowed (Apple Books behaviour) but not a goal; no cross-window sync work.

### B. Toolbar & controls (replaces the bar)
- Floating, minimal, Apple Books-style. Layout:
  - Left: traffic-light inset + pill `[contents · notes/highlights]`.
  - Center: book title (single line, truncate).
  - Right: a small passive live/buffer status dot → pill `[share · Aa · search]` → `bookmark` circle.
- Grouped pills: subtle translucent background, hairline separators between icons.
- **No AI Film toggle** (removed per user request 2026-06-29). The film is always-on — there is no
  user on/off control; real spend stays gated by the backend `KINORA_LIVE_VIDEO` + budget. Treat
  `generateVideo` as always true wherever the session needs it.
- **Displaced Kinora controls** (so nothing covers the video):
  - Live/buffer state → a small **passive status dot** (non-interactive; driven by
    `session.bufferAhead` / `session.live`), replacing the "Buffered Ns ahead" pill. Not a toggle.
  - Bookmark → bookmark circle (same localStorage behaviour).
  - Highlight mode + saved highlights → folded into the notes surface / `Aa` popover area.
- The `Aa` button opens the themes/text popover (section D).
- Search and share are new affordances; for v1, search can scope to in-book text (best-effort, reuse
  existing text), share can be a minimal stub if no API exists — confirm during planning, keep v1 lean.

### C. Scrolling overhaul (buttery continuous scroll)
- Keep continuous scroll; remove anything that fights native momentum (no scroll-jacking,
  no transform hacks on the scroll container).
- Replace the heavy 62% dim of inactive paragraphs with a **gentle** focus (subtle opacity/weight),
  or remove dimming entirely if it reads cleaner — bias toward calm, Apple Books-like text.
- Make paragraph-metric caching robust to resize (recompute on layout change without visible jank;
  fix the "resize mid-scroll misaligns until next ResizeObserver" gap).
- Keep the film scroll-synced (now on the left), but make scrub→play handoff snappier/smoother;
  preserve the ~30Hz seek throttle to avoid seek thrash.
- Respect reduced-motion preferences.

### D. Reading themes (`Aa` popover)
- Rehouse the **existing** prefs (Dark/Night/Sepia/Paper + font/size/leading/measure/spacing/brightness
  + TTS) into a clean Apple Books-style popover anchored to `Aa`. No new theme engine; reuse
  `readingPrefs.ts` + `ReadingControls.tsx` logic, restyled.

### E. MiniMax (Hailuo) video backend + hard $30 cap
- New `backend/app/providers/minimax.py` → `MiniMaxVideoProvider` implementing `VideoBackend`:
  - Base URL `https://api.minimax.io/v1`; auth `Authorization: Bearer <MINIMAX_API_KEY>` (no GroupId
    for the intl host).
  - Submit `POST /v1/video_generation` — body `{model, prompt, duration, resolution}`; for
    image-to-video add `first_frame_image` (public URL or `data:image/...;base64,...`). Response field
    `task_id`.
  - Poll `GET /v1/query/video_generation?task_id=<id>` (~10s cadence): status in
    `Preparing|Queueing|Processing|Success|Fail`; on `Success` read `file_id`.
  - Retrieve `GET /v1/files/retrieve?file_id=<id>` → `response.file.download_url` (expires ~9h) →
    **download bytes and persist to object storage** (reuse the render pipeline's existing
    download-and-persist behaviour required for Wan).
  - Reuse `ProviderClient.request_json`/`download` for resilience where practical.
  - Map Kinora `WanSpec` modes: TEXT_TO_VIDEO → t2v body; IMAGE_TO_VIDEO / REFERENCE_TO_VIDEO →
    `first_frame_image` from the keyframe. Default clip duration 6s, resolution 768P.
  - Raise `LiveVideoDisabled` when `kinora_live_video` is off (before any network call).
  - `name = "minimax:<model>"`; `healthy()` returns True cheaply when live video is off.
- Config (`core/config.py`):
  - `video_backend: str = "dashscope"` (`"dashscope" | "minimax"`).
  - `minimax_api_key: str | None = None`, `minimax_base_url: str = "https://api.minimax.io/v1"`.
  - `minimax_video_model: str = "MiniMax-Hailuo-2.3-Fast"`, `minimax_resolution: str = "768P"`,
    `minimax_duration_s: int = 6`.
  - `minimax_cost_per_clip_usd: float = 0.19`, `budget_ceiling_usd: float = 30.0`.
- Selection: branch in `create_providers()` (L147) on `video_backend` — build `MiniMaxVideoProvider`
  vs `VideoProvider`. (The `ClipGenerator` seam at generator.py L193 remains as a test injection point.)
- **$30 enforcement (belt + suspenders):**
  1. Primary: keep the existing seconds-based budget. Set `budget_ceiling_video_s` to the
     **$30-equivalent** (≈ `budget_ceiling_usd / minimax_cost_per_clip_usd * minimax_duration_s`
     ≈ 30/0.19*6 ≈ **947s ≈ 157 clips**). Each 6s clip charges 6s via the normal reserve/commit path,
     so all existing global/session/scene gating keeps working unchanged.
  2. Hard guard: the MiniMax provider also tracks cumulative USD spend (`clips * cost_per_clip`,
     persisted so restarts don't reset it) and **refuses to submit** once the next clip would cross
     `budget_ceiling_usd`. This protects against duration drift / config mistakes.
- Per-session (300s) and per-scene (90s) caps remain; fine for a small test, revisit only if needed.

### F. Test video (gated, last)
- Preconditions: C + E merged and verified offline.
- Turn `KINORA_LIVE_VIDEO=1` **only** for a controlled run; pick one short passage from a seeded book
  (81 books already seeded). Generate a **handful of 6s clips (~$0.20–$1)** — not the whole book.
- Verify the clip persists to object storage and plays in the new reading window (film on the left,
  scroll-synced). Confirm tracked spend. Then set `KINORA_LIVE_VIDEO` back to 0.
- Report exact spend before and after; never let the run approach $30.

## 5. Data flow (book open → film)

1. Library click → `window.kinora.openBook(id)` → main process `createWindow('#/book/'+id)` + dim library.
2. Book window loads renderer at `#/book/:id` → mounts reading room (toolbar + left film + right text).
3. Reading-room session (existing SSE) streams `clip_ready` / `buffer_state` / `agent_activity`.
4. Scroll position → focus word → film segment/time (existing `useScrollFilm`, overhauled).
5. With live video on + MiniMax selected: pipeline reserves budget → `MiniMaxVideoProvider.render` →
   submit/poll/retrieve → download + persist → clip URL → film plays.

## 6. Testing & verification

- **Backend (offline, no spend):** unit tests for `MiniMaxVideoProvider` (submit body for t2v + i2v,
  poll status mapping, retrieve→download_url extraction, `LiveVideoDisabled` when gated, USD guard
  refuses past ceiling) using mocked HTTP. `video_backend` selection test in `create_providers`.
  Budget math test for the $30→seconds mapping. Run via `backend/.venv/bin/pytest`.
- **Renderer:** `pnpm --filter @kinora/desktop run typecheck && test`. Component/interaction checks for
  the new toolbar and scroll behaviour where practical; drive `:5173`/the window via the project's
  Playwright/chromium to confirm the window, toolbar, and left-film layout render (screencapture is
  blocked, so verify by driving the renderer).
- **Electron:** confirm `openBook` opens a separate window, library dims, close returns focus.
- **Live (gated):** section F, minimal spend, with before/after spend reported.

## 7. Phasing

1. **C — scrolling overhaul** (lands first).
2. **A + B + D — window, toolbar, themes popover.**
3. **E — MiniMax backend + budget cap** (offline-tested).
4. **F — gated test video** (tiny spend).

## 8. Out of scope (YAGNI)

- Pagination / page-turn mode.
- New theming engine or per-book themes.
- Cross-window state sync; multi-book orchestration.
- Subject-reference (`S2V-01`) character consistency (note for later; not in v1).
- Local Wan / `VIDEO_BACKEND=local`.
- Pursuing the unverified 512P/$0.08 MiniMax path.
- Removing/rewriting the Wan/DashScope provider (it stays; MiniMax is additive).

## 9. Risks & mitigations

- **Real money:** dual cap (seconds ceiling + hard USD guard) + tiny gated test + spend reporting.
- **MiniMax URL expiry (9h):** download + persist immediately (same as Wan).
- **i2v image rules:** JPG/PNG, aspect 2:5–5:2, short side >300px, ≤20MB — validate/normalize keyframes
  before submit.
- **Window frame cross-platform:** `hiddenInset` is macOS; keep existing Win/Linux window config paths
  intact and test the book window doesn't regress non-mac chrome.
- **Scroll regressions:** the film sync is tightly coupled to scroll; keep changes behind tests and
  verify scrub/play handoff after the overhaul.
- **Rate limits:** MiniMax 429s are account-gated; back off on 429 (reuse client resilience).
