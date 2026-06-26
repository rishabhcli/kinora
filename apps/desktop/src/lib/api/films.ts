// Film API — the typed client for Agent 1's stitched event/scene films + sync
// maps (the §9.6 "stitch + ship" boundary). The contract is authoritative in
// `coordination/CONTRACTS.md` §Agent-03.
//
// Every type mirrors the backend JSON 1:1 (snake_case), so the SAME objects
// arriving via REST (`getEvents` / `getSceneFilm`) and via SSE
// (`scene_stitched` / `event_stitched`) share one type set — Agent 2's
// ScrollFilmEngine consumes them with no adapter.
import { toBrowserUrl } from "../api";
import { http } from "./http";

/** One narrated word: film-timeline timing + page geometry for the karaoke highlight (§9.4). */
export interface SyncWord {
  word_index: number; // global word index; ties to the page word_boxes + source-span index
  text: string;
  t_start: number; // film-timeline seconds
  t_end: number;
  bbox: [number, number, number, number] | null; // normalized [x,y,w,h], or null
}

/** One shot's window on the film timeline. The core fields
 *  `{shot_id, scene_id, word_range, t_start_s, t_end_s}` are always present;
 *  `{page, page_turn_at_s, words}` are the §9.4 read-along enrichment. */
export interface FilmSyncSegment {
  shot_id: string;
  scene_id: string;
  word_range: [number, number]; // [start, end] inclusive, global word-index
  t_start_s: number; // segment start on the film timeline (== render video_start_s)
  t_end_s: number;
  page: number;
  page_turn_at_s: number; // when the SyncEngine flips the PDF (slightly before t_end_s)
  words: SyncWord[];
}

/** The ordered segments for one film (scene or event), film-timeline seconds. */
export interface FilmSyncMap {
  scene_id: string; // the scene/event id (== event_id at the event level)
  duration_s: number;
  segments: FilmSyncSegment[]; // ordered by t_start_s (reading order)
}

/** Lightweight pointer to a scene composing an event. */
export interface SceneRef {
  scene_id: string;
  scene_index: number;
  word_range: [number, number];
  stitched: boolean;
  duration_s: number | null;
}

/** One scene's film — the partial-load unit. */
export interface SceneFilm {
  scene_id: string;
  event_id: string; // == scene_id today
  book_id: string;
  scene_index: number;
  event_index: number; // == scene_index today
  page_start: number;
  page_end: number;
  word_range: [number, number];
  stitched: boolean; // true iff the stitched mp4 exists in the object store
  oss_url: string | null; // presigned GET URL (null until stitched); rewrite via toBrowserUrl()
  url_expires_at: string | null; // ISO-8601 UTC; null when public (non-expiring)
  duration_s: number | null; // film duration; null when no accepted shots (sync_map.duration_s is 0.0)
  shot_count: number;
  sync_map: FilmSyncMap;
}

/** One event's film — the reader-facing continuous film (== scene 1:1 today). */
export interface EventFilm {
  event_id: string; // == scene_id today
  event_index: number; // == scene_index today
  book_id: string;
  page_start: number;
  page_end: number;
  word_range: [number, number];
  stitched: boolean;
  oss_url: string | null;
  url_expires_at: string | null;
  duration_s: number | null; // film duration; null when no accepted shots (sync_map.duration_s is 0.0)
  shot_count: number;
  sync_map: FilmSyncMap; // event-level (== the single scene's map today)
  scenes: SceneRef[]; // composing scenes (today: [the event's own scene])
}

/** Open-book context for restoring a reading session (§5.2). */
export interface RestoreState {
  session_id: string;
  focus_word: number; // last reading position (global word index)
  current_event_index: number | null; // event (scene) index containing focus_word
  current_scene_id: string | null;
  mode: string; // "viewer" | "director"
}

/** `GET /api/books/{book_id}/events` — all events + open-book restore state. */
export interface EventsResponse {
  book_id: string;
  url_ttl_s: number; // presigned-URL lifetime in seconds; re-fetch before it elapses
  events: EventFilm[]; // ordered by event_index
  restore: RestoreState | null;
}

/** §5.6 SSE frame: a scene's stitched film is ready (replace per-shot playback). */
export interface SceneStitchedEvent {
  event: "scene_stitched";
  scene_id: string;
  oss_url: string;
  sync_map: FilmSyncMap;
}

/** SSE frame: an event-level film is ready (event == scene today). */
export interface EventStitchedEvent {
  event: "event_stitched";
  event_id: string;
  oss_url: string;
  sync_map: FilmSyncMap;
}

export const films = {
  /** All events (scenes) for a book + open-book restore state. */
  getEvents: (bookId: string): Promise<EventsResponse> =>
    http.get<EventsResponse>(`/api/books/${encodeURIComponent(bookId)}/events`),

  /** One scene's film (partial load). */
  getSceneFilm: (bookId: string, sceneId: string): Promise<SceneFilm> =>
    http.get<SceneFilm>(
      `/api/books/${encodeURIComponent(bookId)}/scenes/${encodeURIComponent(sceneId)}/film`,
    ),

  /** Browser-reachable URL for a stitched film (minio:9000 -> localhost rewrite). */
  filmUrl: (film: { oss_url: string | null }): string => toBrowserUrl(film.oss_url),
};
