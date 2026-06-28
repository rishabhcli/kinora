# Library + Director Studio — Design & Roadmap

> Domain owner: the **Library + Director Studio** agent.
> Owned files: `apps/desktop/src/components/` (incl. the new `components/director/`)
> and `apps/desktop/src/lib/api/`. Additive-only on the shared seam
> `apps/desktop/src/lib/api.ts`. **Never** touches `apps/desktop/src/reading/`
> (a different domain owns the reading room).

This is the **second major section** of the desktop app beside the reading room:
a film-editing + library-organization surface over the *same* FastAPI backend.
The reading room is for *watching the book*; the Director Studio is for *shaping
the film* and the library surface is for *organizing, analyzing, and sharing* it.

---

## 1. Architecture

### 1.1 Layering

```
components/                      ← UI (owned)
  LibraryPage.tsx                ← library shelf + Director-mode toggle + studio launch
  director/                      ← NEW: the Director Studio component tree
    DirectorStudio.tsx           ← full-screen workspace shell (tabs, SSE, session mgmt)
    SceneTimeline.tsx            ← §5.4 scene/shot timeline (per-scene lanes)
    ShotInspector.tsx            ← per-shot clip preview + re-roll + region comment
    RegionCommentBar.tsx         ← §5.4 region comment → REST regen
    CanonVault.tsx               ← §8.1 canon viewer + §8.7 surgical-regen editor
    ConflictPanel.tsx            ← §7.2 crew-dispute resolver
    ThreadPanel.tsx              ← collaborative annotation threads
    AnalyticsDashboard.tsx       ← reading analytics + learned directing style
    SharePanel.tsx               ← sharing + export
    types.ts                     ← shared studio types
lib/api/                         ← typed client + pure logic (owned)
  director.ts                    ← typed §5.4/§7.2/§8.x endpoints + pure timeline helpers
  collections.ts                ← faceted search, multi-key sort, smart collections
  analytics.ts                  ← reading-event log + pure pace/time/completion math
  annotations.ts                ← local-first annotation threads + portable export
  sharing.ts                    ← deep links + export bundles + canon→markdown
lib/api.ts                       ← SHARED SEAM (additive-only; see §5)
```

The API layer is split between **typed endpoint wrappers** (thin, over the shared
`http` primitive) and **pure, synchronously-testable logic** (sorting, faceting,
analytics math, thread ops). The pure layer carries the heavy unit-test weight;
the components stay declarative and lean on it.

### 1.2 The §5.4 region-comment contract (VERIFIED against `backend/app/api/routes/director.py`)

A region-comment that should **re-render** a shot must POST to the REST endpoint
`POST /api/sessions/{id}/comment` (`CommentRequest{shot_id, note, region_png?}`).
That route both **classifies** the note (Cinematographer vs Continuity) **and
enqueues a targeted regen** (re-rolling the seed). The WebSocket `comment`
message only *classifies* — it does **not** regenerate. Therefore:

- `RegionCommentBar` and the `ShotInspector` re-roll button both call
  `director.comment(...)` / `director.reroll(...)` → the REST path.
- We never use the WS comment for regen.

This matches the project memory note "Director comment: REST not WS".

### 1.3 Backend endpoints consumed (all verified to exist)

| Endpoint | Used by | Effect |
|---|---|---|
| `POST /sessions/{id}/comment` | RegionCommentBar, ShotInspector | classify + **regen** a shot (§5.4) |
| `POST /books/{id}/canon_edit` | CanonVault | new entity version + **surgical** dependent regen (§8.7) |
| `POST /sessions/{id}/conflict_choice` | ConflictPanel | resolve a §7.2 conflict |
| `GET /sessions/{id}/conflicts` | ConflictPanel | conflict history |
| `GET /books/{id}/shots` | SceneTimeline, CanonVault | the shot timeline |
| `GET /books/{id}/canon` | CanonVault, SharePanel | canon entities + states + markdown vault |
| `GET /books/{id}/prefs`, `GET /me/prefs` | AnalyticsDashboard | learned directing style (§8.6) |
| `POST /sessions` | DirectorStudio | open a session so the live tools work |
| SSE `/sessions/{id}/events` | DirectorStudio | live `agent_activity`/`regen_done`/`clip_ready` |

### 1.4 KINORA_LIVE_VIDEO stays OFF

The studio is exercised end-to-end with the live gate off: comments/edits enqueue
real regens, the render pipeline produces Ken-Burns mp4s, and the SSE
`regen_done`/`clip_ready` swaps the fresh clip into the inspector — no Wan credits
spent. The UI never assumes a session must be live to *browse* (timeline + canon
load read-only); the live tools are gated on a session id, opened lazily.

---

## 2. Local-first subsystems (no backend endpoint yet)

Three subsystems have no backend endpoint today, so they are **local-first** with
a wire format designed to sync to a future backend with no UI change:

- **Annotations / threads** (`annotations.ts`) — persisted to `localStorage`,
  with a versioned portable export (`AnnotationExport v1`). The `{id, author, at}`
  fields are server-authoritative-ready; a future `GET/POST /books/{id}/annotations`
  swaps the store's backing with the same shapes.
- **Reading-event analytics** (`analytics.ts`) — an append-only reading-event log
  (ring-buffered) drives pace/time/completion/streak math. A future
  `GET /me/analytics` becomes an alternate input to the same pure `summarize()`.
- **Smart collections** (`collections.ts`) — saved rule-based shelves persisted
  locally; purely a client concept (re-evaluated against the live library).

All three use the same injectable-`KeyValueStore` + subscriber pattern as the
existing `lib/settings.ts`, so they are DOM-free testable and store-swappable.

---

## 3. Component integration

`LibraryPage` owns the studio launch: a **Director-mode toggle** in the header.
With it on, opening a *live* (backend-driven) book launches `DirectorStudio` as a
lazily-loaded full-screen overlay; otherwise the existing `onOpenBook` reading-room
flow runs unchanged. This keeps the integration entirely inside owned files — no
edit to `HomePage`, the nav shell, or `reading/`.

`DirectorStudio` is the workspace shell: tabbed (Timeline / Canon / Conflicts /
Notes / Analytics / Share), owns the data loads, manages the (lazy) session, and
subscribes to the session SSE to track which shots are re-rendering — swapping in
the fresh clip on `regen_done`.

---

## 4. Phased roadmap

### ✅ Phase 1 — API layer (DONE)
- `director.ts`: typed §5.4/§7.2/§8.x endpoints + pure timeline helpers
  (`sortShotsByReadingOrder`, `buildSceneLanes`, `canonEditBlastRadius`, …).
- `collections.ts`: faceted search, multi-key sort, smart collections + store.
- `analytics.ts`: reading-event store + pure pace/time/completion/streak math.
- `annotations.ts`: thread model + ops + local store + portable export/import.
- `sharing.ts`: deep links, export bundles, canon→markdown.
- 49 unit tests, all green.

### ✅ Phase 2 — Director Studio UI (DONE)
- `DirectorStudio` shell with tabs + SSE-driven live regen state + lazy session.
- `SceneTimeline`, `ShotInspector` (re-roll), `RegionCommentBar` (REST regen),
  `CanonVault` (surgical regen), `ConflictPanel` (§7.2), `ThreadPanel`,
  `AnalyticsDashboard`, `SharePanel`.
- Wired into `LibraryPage` via the Director-mode toggle.
- 16 component tests, all green.

### ✅ Phase 3 — Faceted library surface (DONE)
- `LibraryWorkbench` / `FacetSidebar` / `CollectionRail` / `SmartCollectionEditor`:
  drive the new faceted search + smart collections from `collections.ts` as a
  full library-organization view, with persisted facet state.

### ✅ Phase 4 — Annotation hub + analytics depth (DONE)
- `AnnotationHub`: a book-wide thread browser (filter by open/resolved/tag,
  jump-to-anchor) over the local store; tag editing.
- `analytics.ts` deepened: per-day series, per-book pace, streak math, and a
  `readingHeatmap` weeks × 7 grid with auto-scaled intensity.
- `ReadingHeatmap`: a calendar-style daily-activity grid.

### ✅ Phase 5 — Shared stores + cross-domain integration seam (DONE)
- `lib/api/stores.ts`: app-wide lazy singletons (`annotationStore`,
  `analyticsStore`, `collectionStore`) so the studio + reading room observe one
  instance live, plus `recordReading(bookId, words, seconds)` — the documented
  zero-coupling seam the reading-room domain calls on each session tick (§5.3).
- `DirectorStudio` now uses the singletons (tests still inject their own).

### ▢ Phase 6 — Backend annotation sync (BLOCKED on a backend endpoint)
- When `POST/GET /books/{id}/annotations` lands, add `annotations.remote.ts`
  that mirrors the local store's interface and swaps the backing. The export
  bundle is already the wire format. **Cross-domain: needs a backend agent.**

### ▢ Phase 7 — Collaborative presence + real multi-user threads
- Author identity from the auth session (today: a passed-in display name).
- Optimistic concurrency + conflict-free merge on the thread comment list.
- Requires the Phase-6 backend + a presence channel (likely the existing SSE).

### ▢ Phase 8 — Timeline editing (re-order / trim / splice)
- The current timeline is read+re-roll. True cut editing (reorder shots, trim
  durations, splice) needs new backend mutation endpoints
  (`PATCH /shots/{id}`, scene re-sequencing) — **cross-domain: backend.**

### ▢ Phase 9 — Export to a real film file
- `SharePanel` exports canon/notes/project JSON today. Rendering a stitched mp4
  export needs the backend stitch endpoint to expose a whole-book film URL.

---

## Verification (this run)

`pnpm --filter @kinora/desktop run typecheck` → exit 0.
`pnpm --filter @kinora/desktop run test` → 183 vitest tests + 9 node-test files, all pass.
`pnpm --filter @kinora/desktop run build` → exit 0; `DirectorStudio` (47.6 kB) and
`LibraryWorkbench` (14 kB) split into their own lazy chunks (no first-paint bloat).

---

## 5. Cross-domain contract changes (record EVERYTHING here)

### 5.1 Shared seam `apps/desktop/src/lib/api.ts`
- **No edits made.** All new modules import the existing public surface only:
  `http`, `toBrowserUrl`, `api`, `auth`, `ApiError`, and the `SessionEvent` type.
  The `http` primitive (the documented Agent-12 seam) is used directly by
  `director.ts`; no additive changes were required.

### 5.2 New owned modules (no coordination needed — inside the domain)
- `lib/api/{director,collections,analytics,annotations,sharing}.ts` + their tests.
- `components/director/*` (the whole subtree) + tests.
- `LibraryPage.tsx`: additive Director-mode toggle + lazy studio overlay; the
  existing `onOpenBook` reading-room path is preserved unchanged.

### 5.3 Requests to OTHER domains (not blocking this work)
- **Backend agent:** an annotations collection endpoint
  (`GET/POST /books/{id}/annotations`) would let Phase 5 swap the local store for
  a synced one with no UI change. Until then, local-first is the honest design.
- **Reading-room agent:** to populate reading analytics with real pace data, the
  reading room should call `createAnalyticsStore().record({book_id, words, seconds})`
  on each session tick. The store is shared via `localStorage`, so this is a
  zero-coupling integration — documented here, not required for the studio.

---

## 6. Testing

- Pure logic: `lib/api/*.test.ts` (vitest) — sorting, faceting, analytics, threads,
  sharing, timeline helpers.
- Components: `components/director/*.test.tsx` (RTL + vitest) — the API client is
  mocked; tests assert the REST regen path, surgical-regen reporting, conflict
  resolution, and thread CRUD.
- Gate: `pnpm --filter @kinora/desktop run typecheck && run test` must stay green.
