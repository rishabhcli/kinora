# Kinora desktop — E2E, visual-regression, a11y & perf harness

A comprehensive Playwright harness for the desktop renderer (`apps/desktop`).
It is **hermetic by default**: every spec runs against a deterministic network
mock, so it needs **only the Vite dev server** — no FastAPI backend, no Docker,
no Wan credits. `KINORA_LIVE_VIDEO` stays **OFF** throughout.

> This harness is **additive**. It lives entirely under `apps/desktop/e2e/` and
> has its own Playwright config (`e2e/playwright.e2e.config.ts`). The pre-existing
> a11y/walkthrough specs (`e2e/a11y.spec.ts`, `e2e/app-screens.spec.ts`,
> `e2e/walkthrough.spec.ts`) and their `apps/desktop/playwright.config.ts` are
> untouched and still run via `npm run test:a11y`.

## Quick start

```bash
cd apps/desktop
pnpm install                     # from the repo root the first time
npm run e2e:install-browsers     # once: playwright install --with-deps chromium

# Run the whole hermetic suite (boots/uses the Vite dev server automatically):
npm run e2e

# Just the canary journey:
npm run e2e:smoke

# Visual regression only:
npm run e2e:visual

# Typecheck the suite (no browser):
npm run e2e:typecheck

# Prove the specs load without running them:
npx playwright test -c e2e/playwright.e2e.config.ts --list
```

The config's `webServer` block runs `npm run dev:web` and **reuses** an
already-running `:5173`, so a dev server you already have up is fine.

## What's covered

| Spec | Surface |
|---|---|
| `specs/smoke.spec.ts` | `@smoke` — the full reader journey end to end |
| `specs/auth.spec.ts` | login, demo entry, register toggle, offline + auth-reject fallback |
| `specs/navigation.spec.ts` | top-nav screen switching, profile menu, logout |
| `specs/library.spec.ts` | library heading/search/sort, upload affordance, book cards |
| `specs/upload.spec.ts` | PDF upload → uploads-in-progress list + status lifecycle |
| `specs/reading-room.spec.ts` | open/close (Escape + Back), scroll-film scrub, film pane, settings popover, AI-film toggle |
| `specs/film-sync.spec.ts` | the **live SSE path** via the mocked EventSource (`buffer_state`/`clip_ready`/`agent_activity`) |
| `specs/director.spec.ts` | Director Studio tabs, §5.4 region-comment bar (disabled offline), close |
| `specs/visual.spec.ts` | visual-regression snapshots (login/home/library/director) with masking |
| `specs/a11y.audit.spec.ts` | axe-core WCAG 2.0/2.1/2.2 A+AA per screen, zero serious/critical |
| `specs/perf.spec.ts` | FCP/LCP/long-task jank against soft budgets |
| `specs/live-backend.spec.ts` | **gated** real-backend smoke (off by default) |

## Architecture

```
e2e/
  playwright.e2e.config.ts   dedicated config (own testDir, snapshots, reporters)
  tsconfig.json              typecheck config for the suite
  fixtures/
    test.ts                  extended `test` — wires mock + page objects + perf + freeze
    seed.ts                  deterministic seed library (BookResponse/PageResponse/ShotResponse)
  mocks/
    apiMock.ts               route interception over the API base + EventSource SSE shim
  pageobjects/               page-object model (Login/Home/Library/Upload/ReadingRoom/Director)
  support/
    selectors.ts             resilient selector vocabulary (roles, text, stable hooks)
    stabilize.ts             freeze motion/clock/random + per-snapshot freeze
    perf.ts                  PerformanceObserver + metrics + soft budgets
    axe.ts                   axe-core wrapper + report writer
    flags.ts                 client localStorage feature flags (AI-film toggle)
  harness/
    director.html/.tsx       mounts the REAL DirectorStudio standalone (deterministic)
    library.e2e.html/.tsx    mounts the REAL LibraryPage standalone (deterministic)
  specs/                     the spec files
  visual/__screenshots__/    committed baselines, namespaced by {platform}
```

### Why two harness mounts?

The live in-app navigation into the **library** and **director** surfaces uses
framer-motion `AnimatePresence` crossfades that are unreliable headless (the
project's own `app-screens.spec.ts` documents the same for the library). So those
two surfaces are also driven via standalone mounts of the **real** components —
exactly like the a11y harness mounts `ReadingControls`/`ReadAloudView`. The
login → home → reading-room flow runs against the **real app** (`/`).

### Determinism

- **Network**: `ApiMock` intercepts `${API_BASE}/api/**` and serves the seed
  library. The SSE `/events` stream is faked by monkeypatching `EventSource` in
  the page; frames are pushed on demand via `api.pushEvent(...)`. The mock is
  installed inside the `page` fixture **before any navigation**, so calls never
  leak to a real backend that happens to be on `:8000`.
- **Motion/time/random**: `freezeMotion` pins the clock (stable greeting),
  seeds `Math.random` (stable cover gradients), forces `prefers-reduced-motion`,
  and disables animations/transitions. Visual snapshots additionally hide
  `<video>` and mask ambient canvases + cover art.

## Modes (env)

| Env | Default | Effect |
|---|---|---|
| `KINORA_E2E_MOCK` | `1` | `0` disables the mock (specs hit the dev server's configured backend) |
| `KINORA_E2E_LIVE` | unset | `1` un-skips `live-backend.spec.ts` (needs `KINORA_E2E_MOCK=0`) |
| `KINORA_E2E_BASE_URL` | `http://localhost:5173` | dev-server URL |
| `KINORA_PERF_FCP` / `_LCP` / `_LONGTASK` | 4000 / 6000 / 2500 (ms) | perf budgets |

### Real-backend smoke (optional)

```bash
# Bring up the stack + seed a demo book first (see repo README): make stack-up && make seed-demo
cd apps/desktop
KINORA_E2E_MOCK=0 KINORA_E2E_LIVE=1 \
  VITE_KINORA_API_URL=http://localhost:8000 \
  npm run e2e -- e2e/specs/live-backend.spec.ts
```
`KINORA_LIVE_VIDEO` must stay **OFF** — the live smoke only checks the renderer
talks to a real API; it never triggers Wan generation.

## Visual baselines

Baselines are committed under `e2e/visual/__screenshots__/.../{platform}/`.
`darwin/` is for local macOS dev; CI seeds `linux/` in the pinned Playwright
container (`mcr.microsoft.com/playwright:v1.61.1-jammy`). To re-baseline:

```bash
npm run e2e:update-snapshots
```

Tolerances live in `playwright.e2e.config.ts` (`maxDiffPixelRatio: 0.02`, more on
media-heavy screens) to absorb sub-pixel AA.

## CI

`.github/workflows/e2e-desktop-harness.yml` runs the functional/a11y/perf suite
and the visual job in the pinned container. Reports + axe/perf JSON are uploaded
as artifacts. The shared `ci.yml`'s `e2e-desktop` job invokes
`pnpm --filter @kinora/desktop run e2e`, which now resolves to this suite.

## Notes / known issues

- The Director Studio **Analytics** tab crashes on empty analytics data (reads
  `.length` on undefined, no error boundary), which unmounts the studio. That
  test is `test.fixme(...)` with a pointer to the fix; the other five tabs are
  covered. (Renderer bug — not in this harness's scope.)
- The older a11y harness files (`harness.tsx`/`library.tsx`/`reading.tsx`)
  import a non-existent `@/index.css` and 500 at Vite transform time. This
  harness uses the correct `@/styles/index.css` and does not depend on them.
