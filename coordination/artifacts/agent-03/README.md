# Agent 03 artifacts — Film API, Sync Map & Client Wiring

The HTTP + client boundary between Agent 1's stitched films and Agent 2's scroll engine.

## Deliverables (file map)

| File | What |
|---|---|
| `backend/app/films/contract.py` | Pure wire models (`EventFilm`, `SceneFilm`, `FilmSyncMap`, `FilmSyncSegment`, `SyncWord`, `SceneRef`, `RestoreState`, `EventsResponse`) + builders (`merge_and_build_film_sync_map`, `film_sync_map_from_merged`, `scene_stitched_event`, `event_stitched_event`). No DB/network; decoupled from `render/`. |
| `backend/app/api/routes/films.py` | `GET /api/books/{id}/events`, `GET /api/books/{id}/scenes/{scene_id}/film`. On-read sync-map build from accepted shots; presigned URLs; restore state. |
| `backend/tests/test_films_contract.py` | 7 pure unit tests (merge math, field mapping, SSE payloads). |
| `backend/tests/test_api_films.py` | 7 integration tests (events list, stitched/unstitched, partial load, 404s, restore). |
| `apps/desktop/src/lib/api/films.ts` | TS mirror of every type + `films.getEvents/getSceneFilm/filmUrl` + `Scene/EventStitchedEvent`. |
| `apps/desktop/src/lib/api/http.ts` | Thin typed-GET shim reusing the base client's public exports (Agent 12 supersedes — see `requests/agent-03.md`). |
| `apps/desktop/src/lib/api/films.typecheck.ts` | Compile-time proof Agent 2 consumes the contract with no adapter. |

The authoritative contract is `coordination/CONTRACTS.md` §Agent-03. Example payloads:
`example-responses.json`. Verification evidence: `verification.md`.

## How Agent 2 consumes it (no adapter)

```ts
import { films, type EventFilm, type FilmSyncMap, type SceneStitchedEvent } from "../lib/api/films";

const { events, restore } = await films.getEvents(bookId);
const film = await films.getSceneFilm(bookId, sceneId);
const src = films.filmUrl(film);              // browser-reachable mp4 URL

// scroll -> video seek (§5.2): focus word -> segment -> in-shot time
function seek(map: FilmSyncMap, focusWord: number): number | null {
  for (const seg of map.segments) {
    const [a, b] = seg.word_range;
    if (focusWord >= a && focusWord <= b)
      return seg.words.find((w) => w.word_index === focusWord)?.t_start ?? seg.t_start_s;
  }
  return null;
}
```
A live `scene_stitched` SSE frame and a fetched film share `FilmSyncMap`, so the hot-swap from
per-shot to stitched playback (§9.6) needs no conversion.

## How Agent 12 wires it
Two asks in `coordination/requests/agent-03.md`: register `films.router`; expose `http` from
`lib/api.ts` (then delete the shim, flip one import in `films.ts`).

## Key decisions
- **event ≡ scene (1:1) today**; `EventFilm.scenes[]` forward-compatible for grouping.
- **On-read sync map** from accepted shots → works with `KINORA_LIVE_VIDEO` off (unstitched ⇒
  `stitched:false`, `oss_url:null`, empty segments; never blocks on rendering).
- **`t_start_s`/`t_end_s`** are the canonical names for render's `video_start_s`/`video_end_s`;
  contract adds segment-level `scene_id` + `word_range`.
