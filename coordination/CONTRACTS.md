# CONTRACTS

> Append-only per agent. Each agent owns one section. Agent 12 reconciles at integration.

---

## Agent 03 — Film API, Sync Map & Client Wiring

**Status:** PUBLISHED (v1). Authoritative for the event/scene **film** HTTP boundary, the
**sync map** shape, and the `event_stitched` / `scene_stitched` SSE payloads.

**Owns (publishes):** `backend/app/api/routes/films.py`, `backend/app/films/contract.py`
(pure wire models + builders), `apps/desktop/src/lib/api/films.ts` (TS types + methods).

**Consumes:** Agent 1's stitch output (`app.render.stitch.StitchResult` /
`app.render.sync_map.SyncSegment`) and the object-store keys (`app.storage.object_store.keys`).

### 0. Terminology — "event" ≡ "scene" (1:1 today, forward-compatible)

Kinora's persistent stitch unit is the **scene** (`scenes` table, ordered by `scene_index`;
the §9.6 stitch boundary). There is **no `events` table** and **no event-level video** in the
data model. This API surfaces each scene as the reader-facing **event film** (one continuous
film a reader watches), so:

- `event_id == scene_id`, `event_index == scene_index` **today**.
- `EventFilm.scenes[]` lists the scene(s) composing the event — today exactly one (itself).
- A future Agent-1 event-grouping (several scenes → one event) extends `scenes[]` and the
  event-level `sync_map` **without breaking this contract**.

### 1. Wire conventions

- **JSON is snake_case** (matches existing `ShotResponse`, `BookResponse`, `oss_url`).
- **`word_range` is `[start, end]` inclusive** in global word-index space (matches
  `app.agents.contracts.SourceSpan.word_range` and the §4.2 source-span index).
- **Timeline seconds** (`t_start_s`, `t_end_s`, `page_turn_at_s`, word `t_start`/`t_end`)
  are on the **film timeline** (scene/event timeline after cumulative merge), not per-shot.
- `t_start_s` / `t_end_s` are the canonical names for what `app.render.sync_map.SyncSegment`
  calls `video_start_s` / `video_end_s`. **`t_start_s ≡ video_start_s`, `t_end_s ≡ video_end_s`.**

### 2. Types (the published contract)

```jsonc
// SyncWord — per-word karaoke timing (§9.4). Timings are film-timeline seconds.
SyncWord = {
  word_index: int,            // global word index; ties to source-span index + page word_boxes
  text: string,
  t_start: float,
  t_end: float,
  bbox: [float, float, float, float] | null   // normalized [x,y,w,h] page box, or null
}

// FilmSyncSegment — one shot's window on the film timeline.
// Core fields {shot_id, scene_id, word_range, t_start_s, t_end_s} are REQUIRED;
// {page, page_turn_at_s, words} are the §9.4 enrichment (page-turn + karaoke).
FilmSyncSegment = {
  shot_id: string,
  scene_id: string,
  word_range: [int, int],     // [start, end] inclusive, global word-index
  t_start_s: float,
  t_end_s: float,
  page: int,
  page_turn_at_s: float,      // when SyncEngine flips the PDF (slightly before t_end_s)
  words: SyncWord[]
}

// FilmSyncMap — the ordered segments for one film (scene or event).
FilmSyncMap = {
  scene_id: string,           // the scene/event id this map belongs to (== event_id at event level)
  duration_s: float,
  segments: FilmSyncSegment[] // ordered by t_start_s (reading order)
}

// SceneRef — lightweight pointer to a composing scene (in EventFilm.scenes[]).
SceneRef = {
  scene_id: string,
  scene_index: int,
  word_range: [int, int],
  stitched: bool,
  duration_s: float | null
}

// SceneFilm — GET /api/books/{book_id}/scenes/{scene_id}/film
SceneFilm = {
  scene_id: string,
  event_id: string,           // == scene_id today
  book_id: string,
  scene_index: int,
  event_index: int,           // == scene_index today
  page_start: int,
  page_end: int,
  word_range: [int, int],     // span covered by the scene's accepted shots
  stitched: bool,             // true iff the stitched mp4 exists in the object store
  oss_url: string | null,     // presigned GET URL for the stitched mp4 (null until stitched)
  url_expires_at: string | null,  // ISO-8601 UTC; null when public (non-expiring) or no film
  duration_s: float | null,
  shot_count: int,            // accepted shots in the film
  sync_map: FilmSyncMap
}

// EventFilm — items in GET /api/books/{book_id}/events
EventFilm = {
  event_id: string,           // == scene_id today
  event_index: int,           // == scene_index today
  book_id: string,
  page_start: int,
  page_end: int,
  word_range: [int, int],
  stitched: bool,
  oss_url: string | null,
  url_expires_at: string | null,
  duration_s: float | null,
  shot_count: int,
  sync_map: FilmSyncMap,      // event-level (== the single scene's map today)
  scenes: SceneRef[]          // composing scenes (today: [the event's own scene])
}

// RestoreState — open-book context for Agent 12 to restore (§5.2). null when no prior session.
RestoreState = {
  session_id: string,
  focus_word: int,                  // last reading position (global word index)
  current_event_index: int | null,  // event (scene) index containing focus_word
  current_scene_id: string | null,
  mode: string                      // "viewer" | "director"
} | null

// EventsResponse — GET /api/books/{book_id}/events
EventsResponse = {
  book_id: string,
  url_ttl_s: int,             // presigned-URL lifetime in seconds (see §4)
  events: EventFilm[],        // ordered by event_index
  restore: RestoreState
}
```

### 3. Endpoints

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/api/books/{book_id}/events` | `EventsResponse` | All events (scenes) for a book + restore state. Auth: book owner. |
| GET | `/api/books/{book_id}/scenes/{scene_id}/film` | `SceneFilm` | One scene's film (partial load). Auth: book owner. 404 if scene not in book. |

- Auth: `Authorization: Bearer <jwt>` (same as every route). 404 (`book_not_found` /
  `scene_not_found`) when the book isn't owned by the caller or the scene isn't in the book.
- A film with **no accepted shots yet** returns `stitched:false, oss_url:null` and an empty
  `sync_map.segments` — the endpoint never blocks on rendering (works with `KINORA_LIVE_VIDEO` off).

### 4. Presigned URL lifetime + refresh semantics

- `oss_url` is an **S3/MinIO presigned GET URL** valid for `url_ttl_s` seconds
  (default **3600s**, from `ObjectStore` TTL).
- When `S3_PUBLIC_BASE_URL` is configured (local dev), `oss_url` is a **stable public URL**
  (`{base}/{key}`) that does **not** expire → `url_expires_at` is `null`.
- `url_expires_at` (ISO-8601 UTC) is set for signed URLs so the client knows when to refresh.
- **Refresh pattern:** before `url_expires_at`, re-`GET` the same endpoint to mint fresh URLs.
  For long playback, re-`GET .../scenes/{scene_id}/film`. The `scene_stitched` /
  `event_stitched` SSE frames also carry a fresh `oss_url`.
- The client must rewrite the host for the browser with `toBrowserUrl()` (minio:9000→localhost:9000).

### 5. SSE payloads (WS3) — ride Agent 12's session stream (§5.6)

Event names match §5.6. `sync_map` is a `FilmSyncMap` (canonical shape above).

```jsonc
// scene_stitched — replace per-shot playback with the stitched scene (§9.6)
{ event: "scene_stitched", scene_id: string, oss_url: string, sync_map: FilmSyncMap }

// event_stitched — event-level rollup ready (NEW; event == scene today)
{ event: "event_stitched", event_id: string, oss_url: string, sync_map: FilmSyncMap }
```

Builders live in `backend/app/films/contract.py`. **Exact signatures:**
`scene_stitched_event(*, scene_id, oss_url, sync_map: FilmSyncMap)` and
`event_stitched_event(*, event_id, oss_url, sync_map: FilmSyncMap)`. A producer emits in two
steps — convert the merged render map to a `FilmSyncMap`, then build the frame:

```python
from app.films.contract import film_sync_map_from_merged, scene_stitched_event

# spans: {shot_id: [word_start, word_end]} from each shot's source_span.word_range
fsm = film_sync_map_from_merged(stitched.sync_map, scene_id=stitched.scene_id, spans=spans)
await redis.publish(channel, scene_stitched_event(
    scene_id=stitched.scene_id, oss_url=stitched.clip_url, sync_map=fsm))
```

Emitting via these keeps SSE byte-compatible with REST (no client adapter). **Current state:** the
worker (`app/queue/worker.py`, Agent 1) still emits render-shaped `scene_stitched`
(`video_start_s`/no `word_range`) and nothing emits `event_stitched` yet — so until a producer
adopts the builders the SSE frames do **not** match this `FilmSyncMap` shape on the wire. Wiring
is a cross-seam item in `requests/agent-03.md`.

### 6. Client (films.ts) — Agent 2 consumes this, no adapter

`apps/desktop/src/lib/api/films.ts` exports the TS mirror of every type above plus:

```ts
films.getEvents(bookId: string): Promise<EventsResponse>
films.getSceneFilm(bookId: string, sceneId: string): Promise<SceneFilm>
films.filmUrl(film: { oss_url: string | null }): string   // toBrowserUrl(oss_url) convenience
```

The TS field names/types are identical to the JSON above (snake_case), so the same objects
arriving via SSE (`scene_stitched`/`event_stitched`) and via REST share one type set.
