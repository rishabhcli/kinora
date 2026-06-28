// Director Studio API client (Director domain) — the typed surface for the §5.4
// director tools: region comments (which REGENERATE a shot), surgical canon
// edits, conflict resolution, and the read-only canon-vault + shot-timeline
// loads. Built ONLY on the shared `http` primitive exported from `lib/api.ts`
// (the cross-domain seam); this module never edits that file.
//
// IMPORTANT (verified against backend/app/api/routes/director.py): a region
// comment that should re-render a shot must POST to
// `POST /api/sessions/{id}/comment` (REST). That endpoint classifies the note
// AND enqueues a targeted regen (re-rolling the seed). The WebSocket `comment`
// message only CLASSIFIES — it does not regenerate. The shot re-roll UI and the
// region-comment bar therefore both go through this REST path.
import { http, toBrowserUrl } from "../api";

// ---- Request/response shapes (mirror backend schemas 1:1, snake_case) ----- //

/** A learned directing prior projected as plain language (§8.6). Mirrors the
 *  backend `DirectingPriorView`. Drives the analytics "your directing style". */
export interface DirectingPrior {
  kind: string;
  bias: number; // signed: negative = slower/cooler/closer, positive = faster/warmer/wider
  weight: number;
  label: string;
  detail: string;
  applied: boolean;
  applied_value: string | null;
  last_note: string | null;
}

/** The reader's accumulated directing style for a scope (§8.6). */
export interface DirectingStyle {
  scope: "user" | "book";
  book_id: string | null;
  priors: DirectingPrior[];
}

/** A Director region-comment. `region_png` is an optional base64 PNG of the
 *  marked region (the backend stores it with the regen request). */
export interface CommentRequest {
  shot_id: string;
  note: string;
  region_png?: string | null;
}

/** How a comment routed + the regen it triggered (§5.4). `job_id` is the queued
 *  regen; `learned` is any directing prior the note just taught (usually empty). */
export interface CommentResponse {
  shot_id: string;
  agent: string;
  aspect: string;
  message: string;
  job_id: string | null;
  learned: DirectingPrior[];
}

/** An edit to a canon entity → surgical dependent regen (§5.4/§8.7). */
export interface CanonEditRequest {
  entity_key: string;
  changes: Record<string, unknown>;
  valid_from_beat?: number | null;
}

/** The new entity version + the dependent shots queued for regen (§8.7). */
export interface CanonEditResponse {
  entity_key: string;
  version: number;
  affected_shot_ids: string[];
  skipped_shots: number;
}

/** The fixed §7.2 conflict-resolution options. An unknown value is a 422. */
export type ConflictOption = "honor_canon" | "evolve_canon" | "surface_to_user";

export interface ConflictChoiceRequest {
  conflict_id: string;
  option: ConflictOption;
}

export interface ConflictChoiceResponse {
  conflict_id: string;
  option: ConflictOption;
  /** applied · deferred · already_resolved · recorded */
  status: string;
  shot_id: string | null;
  reasoning: string | null;
}

/** A surfaced conflict + its resolution — the §7.2 history a refresh reloads. */
export interface ConflictRecord {
  conflict_id: string;
  shot_id: string | null;
  claim: string | null;
  canon_fact: string | null;
  raised_by: string | null;
  current_beat: string | null;
  options: Array<Record<string, unknown>>;
  resolved: boolean;
  chosen_option: string | null;
  reasoning: string | null;
}

// ---- Canon vault (read) --------------------------------------------------- //

export interface CanonReferenceImage {
  oss_url: string;
  oss_key: string | null;
  pose: string | null;
  locked: boolean | null;
}

export interface CanonAppearance {
  description: string | null;
  reference_images: CanonReferenceImage[];
}

/** One canon entity (current version) projected for the §5.4 canon editor.
 *  `id` is the stable `entity_key` the canon-edit call targets. */
export interface CanonEntity {
  id: string;
  type: string;
  name: string;
  aliases: string[];
  description: string | null;
  appearance: CanonAppearance | null;
  style_tokens: Record<string, unknown> | null;
  voice: Record<string, unknown> | null;
  version: number;
  valid_from_beat: number | null;
  valid_to_beat: number | null;
  first_appearance: Record<string, unknown> | null;
}

/** A versioned continuity fact (§8.5). `active === false` means it was retired
 *  (the "forgetting" mechanism) — the vault shows both so the story's belief
 *  timeline is inspectable. */
export interface CanonState {
  id: string;
  subject_entity_key: string;
  predicate: string;
  object_value: string;
  valid_from_beat: number;
  valid_to_beat: number | null;
  version: number;
  active: boolean;
  source_span: Record<string, unknown> | null;
}

/** The whole canon graph for a book (§8.1). */
export interface CanonGraph {
  book_id: string;
  entities: CanonEntity[];
  states: CanonState[];
  markdown: string | null;
}

// ---- Shots (the §5.4 scene timeline) -------------------------------------- //

export interface ShotSourceSpan {
  page?: number;
  para?: number;
  word_range?: [number, number];
  [k: string]: unknown;
}

/** A shot's episodic record for the timeline / re-roll inspector. */
export interface DirectorShot {
  shot_id: string;
  beat_id: string | null;
  scene_id: string | null;
  source_span: ShotSourceSpan | null;
  status: string;
  render_mode: string | null;
  duration_s: number | null;
  qa: Record<string, unknown> | null;
  clip_url: string | null;
  reference_image_ids: string[];
}

// ---- Pure timeline helpers (testable, no I/O) ----------------------------- //

/** One scene's lane on the timeline: its shots in reading order + total runtime. */
export interface SceneLane {
  scene_id: string;
  shots: DirectorShot[];
  duration_s: number;
  word_start: number | null;
  word_end: number | null;
}

function shotWordStart(s: DirectorShot): number | null {
  const r = s.source_span?.word_range;
  return Array.isArray(r) ? r[0] : null;
}

function shotWordEnd(s: DirectorShot): number | null {
  const r = s.source_span?.word_range;
  return Array.isArray(r) ? r[1] : null;
}

/** Sort shots by reading position (word_range start, then beat_id, then id) so
 *  the timeline reads top-to-bottom in story order — independent of API order. */
export function sortShotsByReadingOrder(shots: DirectorShot[]): DirectorShot[] {
  return [...shots].sort((a, b) => {
    const aw = shotWordStart(a);
    const bw = shotWordStart(b);
    if (aw !== null && bw !== null && aw !== bw) return aw - bw;
    if (aw !== null && bw === null) return -1;
    if (aw === null && bw !== null) return 1;
    const ab = a.beat_id ?? "";
    const bb = b.beat_id ?? "";
    if (ab !== bb) return ab.localeCompare(bb);
    return a.shot_id.localeCompare(b.shot_id);
  });
}

/** Group shots into per-scene lanes in scene-then-reading order. A null
 *  `scene_id` is bucketed under the literal "(unscened)" lane so nothing is
 *  silently dropped from the timeline. */
export function buildSceneLanes(shots: DirectorShot[]): SceneLane[] {
  const order: string[] = [];
  const byScene = new Map<string, DirectorShot[]>();
  for (const shot of shots) {
    const key = shot.scene_id ?? "(unscened)";
    if (!byScene.has(key)) {
      byScene.set(key, []);
      order.push(key);
    }
    byScene.get(key)!.push(shot);
  }
  const lanes: SceneLane[] = order.map((scene_id) => {
    const laneShots = sortShotsByReadingOrder(byScene.get(scene_id)!);
    const duration_s = laneShots.reduce((sum, s) => sum + (s.duration_s ?? 0), 0);
    const starts = laneShots.map(shotWordStart).filter((x): x is number => x !== null);
    const ends = laneShots.map(shotWordEnd).filter((x): x is number => x !== null);
    return {
      scene_id,
      shots: laneShots,
      duration_s,
      word_start: starts.length ? Math.min(...starts) : null,
      word_end: ends.length ? Math.max(...ends) : null,
    };
  });
  // Order lanes by their earliest word position so the scene list reads in story
  // order; lanes with no positional info sink to the bottom, stable among themselves.
  return lanes
    .map((lane, i) => ({ lane, i }))
    .sort((a, b) => {
      const aw = a.lane.word_start;
      const bw = b.lane.word_start;
      if (aw !== null && bw !== null && aw !== bw) return aw - bw;
      if (aw !== null && bw === null) return -1;
      if (aw === null && bw !== null) return 1;
      return a.i - b.i;
    })
    .map(({ lane }) => lane);
}

/** A shot is "renderable" (a real generated clip plays) iff it has a clip URL
 *  and is in a terminal accepted state. Used by the inspector to label state. */
const TERMINAL_OK = new Set(["accepted", "promoted", "ready", "stitched", "done"]);
export function isShotRenderable(shot: DirectorShot): boolean {
  return Boolean(shot.clip_url) && (shot.status ? TERMINAL_OK.has(shot.status.toLowerCase()) : false);
}

/** Browser-reachable clip URL (minio:9000 -> localhost rewrite, drop presign). */
export function shotClipUrl(shot: DirectorShot): string {
  return toBrowserUrl(shot.clip_url);
}

/** True iff this shot's reference set includes the given canon entity (any
 *  version). Mirrors the backend `_references_entity` so the canon editor can
 *  preview a canon edit's blast radius before POSTing it. */
export function shotReferencesEntity(shot: DirectorShot, entityKey: string): boolean {
  return (shot.reference_image_ids ?? []).some(
    (ref) => ref === entityKey || ref.split("@", 1)[0] === entityKey,
  );
}

/** How many shots a canon edit to `entityKey` would re-render (the §8.7 blast
 *  radius preview). Everything else stays a cache hit. */
export function canonEditBlastRadius(shots: DirectorShot[], entityKey: string): number {
  return shots.filter((s) => shotReferencesEntity(s, entityKey)).length;
}

// ---- The client ----------------------------------------------------------- //

export const director = {
  /** The book's shots (the §5.4 scene timeline) as a bare array. */
  getShots: (bookId: string): Promise<DirectorShot[]> =>
    http<DirectorShot[]>(`/api/books/${encodeURIComponent(bookId)}/shots`),

  /** The book's canon graph (entities + continuity facts + markdown vault). */
  getCanon: (bookId: string): Promise<CanonGraph> =>
    http<CanonGraph>(`/api/books/${encodeURIComponent(bookId)}/canon`),

  /** The session's §7.2 conflict log (surfaced disputes + resolutions). */
  getConflicts: (sessionId: string): Promise<ConflictRecord[]> =>
    http<ConflictRecord[]>(`/api/sessions/${encodeURIComponent(sessionId)}/conflicts`),

  /** The directing style learned for one book (§8.6). */
  getBookStyle: (bookId: string): Promise<DirectingStyle> =>
    http<DirectingStyle>(`/api/books/${encodeURIComponent(bookId)}/prefs`),

  /** The reader's directing style across all their books (§8.6). */
  getMyStyle: (): Promise<DirectingStyle> => http<DirectingStyle>(`/api/me/prefs`),

  /** Post a region-comment → CLASSIFY + REGENERATE the shot (§5.4). This is the
   *  REST path that re-renders; the WS comment message only classifies. Both the
   *  shot re-roll button and the region-comment bar call this. */
  comment: (sessionId: string, body: CommentRequest): Promise<CommentResponse> =>
    http<CommentResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/comment`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Re-roll a shot with no critique — a bare "give me another take". Implemented
   *  as a neutral comment so it goes through the same regen path (§5.4). */
  reroll: (
    sessionId: string,
    shotId: string,
    note = "Give me another take of this shot.",
  ): Promise<CommentResponse> => director.comment(sessionId, { shot_id: shotId, note }),

  /** Edit a canon entity → surgical regen of only the dependent shots (§8.7). */
  canonEdit: (bookId: string, body: CanonEditRequest): Promise<CanonEditResponse> =>
    http<CanonEditResponse>(`/api/books/${encodeURIComponent(bookId)}/canon_edit`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Apply the Director's resolution of a surfaced conflict (§7.2). */
  resolveConflict: (
    sessionId: string,
    body: ConflictChoiceRequest,
  ): Promise<ConflictChoiceResponse> =>
    http<ConflictChoiceResponse>(
      `/api/sessions/${encodeURIComponent(sessionId)}/conflict_choice`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};
