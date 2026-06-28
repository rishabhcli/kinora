# Kinora Mobile (`apps/mobile`) — design & living roadmap

> The React Native + Expo reading experience for Kinora. A book becomes a
> page-synced film that generates itself a few seconds ahead of the reader;
> mobile mirrors the desktop product (`apps/desktop`) but with a vertical,
> gesture-driven scroll↔film sync built for touch.

This file is the authoritative roadmap for the mobile domain. It is **additive**
to the monorepo: it only ever adds `apps/mobile/**`. The root
`pnpm-workspace.yaml` already globs `apps/*`, so we register automatically — no
edit required (see "Shared-file changes").

## Source of truth & what we ported

- **API contract** — ported from `apps/desktop/src/lib/api.ts` (NOT edited) into
  `src/lib/api.ts`. Same response shapes (`BookResponse`, `ShotResponse`,
  `PageResponse`, `SessionResponse`, `BufferState`, `ClipReady`, `SessionEvent`),
  same routes, same `toBrowserUrl` MinIO host-rewrite, same `loginOrRegister`
  fallback, same `ApiError`. Differences forced by RN:
  - **No `localStorage`**. Token lives in Expo `SecureStore` with an in-memory
    fallback (`src/lib/tokenStore.ts`). The store is async, so auth is seeded
    once at boot via `auth.hydrate()`; thereafter `auth.token` is a sync getter.
  - **No `EventSource`**. SSE is parsed by a pure, transport-free line parser
    (`src/lib/sse.ts`) fed by a `fetch` + `ReadableStream` reader. JWT rides in
    the `Authorization` header (RN can set it) AND `?token=` (parity w/ desktop).
  - **Injectable transport** — `req()` resolves `fetch` lazily; the pure parts
    (`parseBody`, `headerMap`, `toBrowserUrl`, `withQuery`) are exported for
    unit tests so no network/native module is touched.

- **Sync / timeline math** — ported from `apps/desktop/src/reading/timeline.ts`,
  `fallback.ts`, `crossfade.ts`, `machine.ts` into `src/sync/*` as **pure,
  DOM-free TS**, byte-for-byte in behaviour (same unit tests pass). Mobile adds:
  - the §5.2 **control-owner token** — the 1.2 s grace window that prevents the
    bidirectional video↔scroll feedback loop — as a pure reducer
    (`src/sync/controlOwner.ts`). Desktop encodes the same idea implicitly via
    velocity scrub/play; mobile makes it explicit because touch gestures and
    `expo-av` playback callbacks fire independently.
  - `src/sync/velocity.ts` — the EMA velocity smoother extracted from the
    desktop `useScrollFilm` rAF loop, pure + tested.
  - `src/sync/engine.ts` — the framework-free `SyncEngine` that owns the playhead,
    arbitrates owner, computes frames, and emits scheduler signals.

## Product mapping (kinora.md §4/§5)

| Desktop concept | Mobile realization |
|---|---|
| §5.1 shelf | `LibraryScreen` — grid of `BookCard`s + pull-to-refresh + upload sheet |
| §5.2 two-pane SyncEngine | `ReadingScreen` — vertical: film stage (sticky top) + scroll text (below). Sync via `SyncEngine` over the scroll handler |
| §5.3 viewer mode | film plays; karaoke word highlight; faint buffer hairline filling toward H |
| §5.4 director mode | `DirectorSheet` — region comment routed via POST `/sessions/{id}/comment` (REST, per project memory: WS only classifies), shot timeline. Scaffolded. |
| §5.6 transport | `src/lib/sse.ts` + `useFilmSession` |
| §4.4 degradation ladder | `resolveFilmSrc` + Ken-Burns over keyframe still when the live clip isn't ready |
| §4.5 dual-watermark buffer | client mirrors `buffer_state` (committed-seconds-ahead toward H=75s) into the hairline |

## Milestones

- [x] **M0 — Scaffold**: Expo managed app (`app.json`, `package.json`, `babel.config.js`,
  `tsconfig.json`, `App.tsx`), jest-expo config, assets placeholder. Workspace auto-registered.
- [x] **M1 — API client + auth/token storage**: `src/lib/api.ts`, `src/lib/sse.ts`,
  `src/lib/tokenStore.ts` (SecureStore + in-memory fallback). Pure parts unit-tested.
- [x] **M2 — Sync core (pure, tested)**: `timeline.ts`, `fallback.ts`, `crossfade.ts`,
  `machine.ts`, `controlOwner.ts`, `velocity.ts`, `engine.ts`. Heavily unit-tested.
- [x] **M3 — State**: `src/state/store.ts` (tiny pub/sub store) + `auth`/`library`/`session`
  slices; `useStore` hook. Tested.
- [x] **M4 — Library + upload flow**: `LibraryScreen`, `BookCard`, `UploadSheet`.
- [x] **M5 — Reading room**: `ReadingScreen`, `FilmStage`, `ReaderColumn` (karaoke),
  `BufferHairline`, `useFilmSession` (SSE live + fallback), `useSyncEngine`.
- [x] **M6 — Reading controls + a11y**: `ReadingControls`, font scaling, reduce-motion,
  screen-reader labels (`src/a11y/*`).
- [x] **M7 — Offline caching**: `src/offline/clipCache.ts` (FileSystem-backed LRU),
  `bookCache.ts` (AsyncStorage metadata). Pure LRU policy tested.
- [x] **M8 — Push notifications scaffold**: `src/notifications/push.ts`.
- [ ] **M9 — Native build/run** (documented; not runnable headless) — see below.

## Verification

- `npx tsc --noEmit` clean for the app (`tsconfig.json`).
- Unit tests via **jest** (preset `jest-expo`, `npm test`): API client (`parseBody`,
  headers, `toBrowserUrl`, `withQuery`, `loginOrRegister` branch), SSE parser,
  all sync math, the state-machine reducer, the control-owner reducer, velocity
  smoothing, the SyncEngine arbitration, the store + slices, and the clip-cache
  LRU policy. **Pure logic only** — tests never import a native module.
- **A full Expo/native build is not run headless** (no simulator/EAS here). Run
  steps below.

## Native build/run (manual)

```bash
make app-install                       # pnpm install (registers apps/mobile)
cd apps/mobile
npx expo start                         # Metro dev server; press i / a / w
# or a native dev build:
npx expo run:ios                       # needs Xcode + a simulator
npx expo run:android                   # needs Android SDK + emulator
```

Point the app at the backend with `EXPO_PUBLIC_KINORA_API_URL` (defaults to
`http://localhost:8000`; on a physical device use your machine's LAN IP, e.g.
`http://192.168.1.x:8000`). `KINORA_LIVE_VIDEO` stays **OFF** on the backend —
the app reads the same bundled fallback film path the desktop uses
(`/generated/film-0N.mp4`) and never spends Wan credits. The "AI film" toggle in
the reading top bar gates the live SSE session exactly like desktop's
`generateVideo`.

## Shared-file changes (additive only)

- `pnpm-workspace.yaml`: `packages: ["apps/*"]` already includes `apps/mobile` —
  **no edit performed**. No new `allowBuilds` entry is required today (Expo's
  install does not need a postinstall approval here); if a future native build
  script needs it, add it under `allowBuilds` and note it here.

## Remaining roadmap (depth, future phases)

- Wire `DirectorSheet` region-comment screenshot capture (`react-native-view-shot`)
  to POST `/sessions/{id}/comment`.
- Canon editor mobile surface (§5.4) + surgical regen via the `regen_done` event.
- Real `expo-av`/`expo-video` playback test on a simulator (M9).
- Background prefetch of the next clip via `expo-task-manager`.
- Haptics on shot boundaries; iOS live-activity buffer state.
