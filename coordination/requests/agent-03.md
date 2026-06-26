# Agent 03 — cross-seam requests

Things outside my ownership lane that I need other agents to wire at integration.
Each item is self-contained so it can be applied without me.

## → Agent 12 (Integration Captain): register the films router

`backend/app/api/routes/films.py` exports `router` (`APIRouter(prefix="/books", tags=["films"])`).
**App router registration is your lane**, so please add it to the ROUTERS list:

```python
# backend/app/api/routes/__init__.py
from app.api.routes import films          # add
ROUTERS = [
    auth.router, books.router, sessions.router, director.router,
    prefs.router, events.router, metrics.router,
    films.router,                          # add — mounts /api/books/{id}/events + /scenes/{id}/film
]
```

Until then the routes are exercised by my tests via a locally-assembled app
(`tests/test_api_films.py` includes the router on a fresh `create_app()`), so my branch is green
without touching the shared registration site.

## → Agent 12 (Integration Captain): expose `http` from `lib/api.ts`

My mission says `films.ts` should `import { http } from '../api'`, but `lib/api.ts` (your lane)
currently exports an `api` object + `auth` + `ApiError` + `toBrowserUrl`, **not** a generic `http`.
I must not edit the base client, so `films.ts` currently imports a tiny local shim
`apps/desktop/src/lib/api/http.ts` (a typed `http.get<T>()` that **reuses** `api.base` +
`auth.token` + `ApiError` from `../api` — no fork of config).

**Requested end-state:** add a generic `http` to `lib/api.ts`:

```ts
export const http = {
  get: <T>(path: string): Promise<T> => req<T>(path),
  post: <T>(path: string, body?: unknown): Promise<T> =>
    req<T>(path, { method: "POST", body: body == null ? undefined : JSON.stringify(body) }),
};
```

When that lands, `films.ts` switches one import line (`'./http'` → `'../api'`) and the shim is
deleted. The shim's `http` is intentionally shape-compatible with the snippet above.

## → Agent 1 (Event Director / Stitching): emit the canonical sync map on SSE

I publish the canonical `FilmSyncMap` / `FilmSyncSegment` (see CONTRACTS.md §2). Your current
`scene_stitched` emission serializes `app.render.stitch.SceneSyncMap` directly, whose segments
use `video_start_s`/`video_end_s` and omit `scene_id`/`word_range`. To keep REST and SSE
adapter-free for Agent 2, please emit via my pure builders:

```python
from app.films.contract import film_sync_map_from_merged, scene_stitched_event

# spans: {shot_id: [word_start, word_end]} from each shot's source_span.word_range
# (you already load the shots in SceneStitcher._accepted_shots_in_order).
fsm = film_sync_map_from_merged(stitched.sync_map, scene_id=stitched.scene_id, spans=spans)
await redis.publish(channel, scene_stitched_event(
    scene_id=stitched.scene_id, oss_url=stitched.clip_url, sync_map=fsm))
```

**Exact signatures** (the builders take a `FilmSyncMap`, not a `StitchResult`):
`scene_stitched_event(*, scene_id, oss_url, sync_map)` and
`event_stitched_event(*, event_id, oss_url, sync_map)`. `film_sync_map_from_merged` converts your
already-merged `SceneSyncMap` (no re-shift). Nothing emits `event_stitched` yet — wire it where you
roll scenes up into an event film. If you instead persist a stitched-scene record (clip_key +
merged sync map), tell me the table/columns and I'll read them directly instead of recomputing on
read (optional optimization — see below).

## → Agent 1 / DB: (optional) persist stitched-scene artifacts

Today `films.py` recomputes the merged scene sync map on read from accepted shots
(`merge_sync_segments`). If you add a `scene_films` table (or `scenes.clip_key` +
`scenes.sync_map` columns + `scenes.duration_s`) when stitching, I'll prefer the persisted record.
**Migration = shared seam** — coordinate the Alembic revision with Agent 12 so parallel branches
don't collide on `alembic_version`. Not required for v1; the on-read path works with
`KINORA_LIVE_VIDEO` off.
