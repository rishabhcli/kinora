// Compile-time proof (WS2) that Agent 2's ScrollFilmEngine can consume the film
// contract with NO adapter. This file is never imported by the app — it exists
// only so `tsc --noEmit` (the desktop typecheck) exercises the public types the
// way Agent 2 will (scroll<->video<->word sync, §5.2/§5.3). If it compiles, the
// contract is directly usable.
import { films } from "./films";
import type {
  EventFilm,
  EventsResponse,
  FilmSyncMap,
  FilmSyncSegment,
  RestoreState,
  SceneFilm,
  SceneStitchedEvent,
} from "./films";

// 1. The API methods return exactly the contract types (no casting).
async function _usesApi(bookId: string, sceneId: string): Promise<SceneFilm> {
  const events: EventsResponse = await films.getEvents(bookId);
  const restore: RestoreState | null = events.restore;
  void restore;
  const first: EventFilm | undefined = events.events[0];
  void first;
  return films.getSceneFilm(bookId, sceneId);
}

// 2. scroll -> video seek: resolve a focus word to an in-shot film time (§5.2).
function _resolveSeek(map: FilmSyncMap, focusWord: number): number | null {
  for (const seg of map.segments) {
    const [start, end] = seg.word_range;
    if (focusWord >= start && focusWord <= end) {
      const w = seg.words.find((x) => x.word_index === focusWord);
      return w ? w.t_start : seg.t_start_s;
    }
  }
  return null;
}

// 3. viewer-mode karaoke: the active word at a playhead time (§5.3).
function _wordAt(seg: FilmSyncSegment, t: number): number | null {
  const hit = seg.words.find((w) => t >= w.t_start && t <= w.t_end);
  return hit ? hit.word_index : null;
}

// 4. A live `scene_stitched` SSE frame and a fetched film share FilmSyncMap —
//    so the hot-swap from per-shot to stitched playback needs no conversion (§9.6).
function _hotSwap(frame: SceneStitchedEvent, fetched: EventFilm): boolean {
  const live: FilmSyncMap = frame.sync_map;
  const rest: FilmSyncMap = fetched.sync_map;
  return live.scene_id === rest.scene_id;
}

// 5. The film URL helper accepts any object with an oss_url (event or scene).
function _url(film: SceneFilm): string {
  return films.filmUrl(film);
}

// Referenced (never executed) so the proof is a real module, not dead syntax.
export const _filmsConsumeProof = { _usesApi, _resolveSeek, _wordAt, _hotSwap, _url };
