# DESIGN.md — Desktop E2E & quality harness (living roadmap)

Domain: a comprehensive end-to-end + quality harness for `apps/desktop`,
living entirely under `apps/desktop/e2e/`. Built to be **additive** (own config,
own specs, no edits to renderer source or the pre-existing a11y specs) and
**hermetic** (network-mocked; `KINORA_LIVE_VIDEO` OFF; no Docker; no Wan credits) —
so it runs fast and clean, anywhere.

## Principles

1. **Resilient selectors.** Role + accessible name + visible text first; a small
   set of stable hooks (`.book-cover`, `[data-testid="reading-scroll"]`,
   `[data-warmup]`, `role="dialog"`/`role="tab"`) second. CSS classes only as a
   last resort. The vocabulary is centralised in `support/selectors.ts` so a UI
   rename is a one-line patch, not a sweep — a real win given the renderer is
   under active multi-agent churn.
2. **Determinism over realism.** A deterministic API mock + EventSource shim
   means specs pass byte-for-byte whether or not the real stack is up. Motion,
   clock, and `Math.random` are frozen for rock-solid visual stability.
3. **Mount the real components.** Where live in-app nav is flaky headless
   (library, director — framer crossfades), drive standalone mounts of the
   *real* components, not fakes, so the core journey still runs against the real app.
4. **Soft gates that catch regressions, not churn.** a11y asserts zero
   serious/critical (records everything); perf asserts generous budgets (catch
   10x regressions). Both write JSON artifacts for easy triage.
5. **Additive shared-file changes only**, documented here (see below).

## Shared-file changes (additive)

- `apps/desktop/package.json` — added scripts only (no edits to existing ones):
  `e2e`, `e2e:ui`, `e2e:headed`, `e2e:smoke`, `e2e:visual`,
  `e2e:update-snapshots`, `e2e:live`, `e2e:typecheck`, `e2e:report`,
  `e2e:install-browsers`. The existing `ci.yml`'s `e2e-desktop` job calls
  `run e2e`, which now resolves here.
- `.github/workflows/e2e-desktop-harness.yml` — **new** workflow (the shared
  `ci.yml` is untouched).
- Nothing else outside `apps/desktop/e2e/` is modified.

## Status — Milestone 1 (DONE)

- [x] Dedicated config `playwright.e2e.config.ts` (own testDir/snapshots/reporters).
- [x] Page-object model: Login, Home, Library, Upload, ReadingRoom, Director.
- [x] Deterministic API mock + SSE EventSource shim (`mocks/apiMock.ts`).
- [x] Seed data mirroring backend contracts (`fixtures/seed.ts`).
- [x] Extended `test` fixture wiring mock + POMs + perf + freeze (`fixtures/test.ts`).
- [x] Specs: auth, navigation, library, upload, reading-room, film-sync,
      director, smoke, visual, a11y audit, perf, live-backend (gated).
- [x] Visual-regression with masking + per-platform baselines (darwin seeded).
- [x] axe-core per-screen audits with artifact reports.
- [x] Perf tracing (FCP/LCP/long-task) with soft budgets + artifacts.
- [x] Standalone harness mounts for library + director (deterministic).
- [x] CI workflow (functional/a11y/perf + pinned-container visual).
- [x] README with run instructions + modes + baseline policy.

**Verification:** `e2e:typecheck` clean; `--list` shows 53 tests in 12 files;
**49 passed / 3 skipped** (2 gated live + 1 documented `fixme`) on the hermetic
run — visual baselines pass deterministically on every re-run.

## Roadmap — next milestones (not yet built)

- **M2 — deeper reading-room coverage**
  - [ ] Keyboard scroll (Arrow/Page/Home/End) advances the active paragraph.
  - [ ] Reading-controls: theme/font/spacing/brightness change persists to CSS vars.
  - [ ] Bookmark + highlight-mode toggles round-trip via localStorage.
  - [ ] Read-aloud word-sync (scripted SpeechSynthesis) inside the real room
        (the existing walkthrough.spec covers the a11y harness version).
  - [ ] Scrub indicator visibility on fast scroll vs settle (crossfade vs hard-cut).
- **M3 — director depth (after the Analytics crash is fixed)**
  - [ ] Start-session → comment bar enabled → submit a region comment → assert
        the §5.4 REST `POST /comment` fires + the routed-agent result renders.
  - [ ] Re-roll enqueues + `regen_done` SSE swaps the clip.
  - [ ] Canon vault edit → surgical regen path.
  - [ ] Un-`fixme` the Analytics tab once it guards empty data (task spawned).
- **M4 — network resilience matrix**
  - [ ] Slow backend (latencyMs) → spinners/skeletons render, no premature error.
  - [ ] 4xx/5xx per endpoint → friendly error states (upload 413/415/429/401).
  - [ ] Offline mid-session → graceful fallback, EventSource reconnect behaviour.
- **M5 — responsive + theme matrix**
  - [ ] Add viewport projects (e.g. narrow) + light/dark `colorScheme` snapshots.
  - [ ] i18n: run a non-English locale and assert key screens still navigate.
- **M6 — perf depth**
  - [ ] CLS + interaction latency (INP-ish) on scroll; bundle-size guard via the
        build output; trace-on-CI upload for flame review.
- **M7 — cross-browser**
  - [ ] Add a webkit/firefox project for the browser-served renderer
        (visual baselines per engine).

## Open questions / risks

- **Visual baselines are OS-specific.** `darwin/` is seeded locally; CI must seed
  `linux/` on first run (the visual job writes-then-passes; commit the result).
- **Renderer churn.** Selectors are centralised, but a wholesale redesign of a
  surface (e.g. the reading-room chrome) will need a `selectors.ts` pass.
- **The two pre-existing broken harness imports** (`@/index.css`) should be fixed
  by the a11y owner; this harness routes around them with its own mounts.
