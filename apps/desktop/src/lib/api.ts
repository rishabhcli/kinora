// Minimal typed client for the Kinora FastAPI backend. There is no generated
// SDK in this repo, so this is the renderer's single API surface. Auth is a
// Bearer token kept in localStorage; every call attaches it when present.
import type { Book } from "../data/books";

// Exported primitive (shared seam, owned by Agent 12): feature API modules in
// src/lib/api/*.ts compose against BASE/auth/http/toBrowserUrl — they never edit
// this file. See coordination/CONTRACTS.md §7.
export const BASE: string =
  (import.meta.env.VITE_KINORA_API_URL as string | undefined) ?? "http://localhost:8000";

const TOKEN_KEY = "kinora.token";
let memoryToken: string | null = null;

function storageCandidates(): Storage[] {
  const stores: Storage[] = [];
  for (const get of [
    () => (typeof window !== "undefined" ? window.localStorage : null),
    () => (typeof window !== "undefined" ? window.sessionStorage : null),
    () => globalThis.localStorage,
    () => globalThis.sessionStorage,
  ]) {
    try {
      const store = get();
      if (store && !stores.includes(store)) stores.push(store);
    } catch {
      /* storage unavailable in this renderer */
    }
  }
  return stores;
}

export const auth = {
  get token(): string | null {
    for (const storage of storageCandidates()) {
      try {
        const token = storage.getItem(TOKEN_KEY);
        if (token) return token;
      } catch {
        /* storage read blocked */
      }
    }
    return memoryToken;
  },
  set token(t: string | null) {
    memoryToken = t;
    for (const storage of storageCandidates()) {
      try {
        if (t) storage.setItem(TOKEN_KEY, t);
        else storage.removeItem(TOKEN_KEY);
      } catch {
        /* storage write blocked */
      }
    }
  },
};

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`API ${status}: ${detail}`);
  }
}

/** Make a MinIO media URL reachable from the renderer: stored clip URLs carry
 *  the internal `minio:9000` host (and sometimes a now-stale presign), so swap
 *  it for the host-facing endpoint and drop the query — the bucket is
 *  public-read for local dev, so the clean URL serves fine. */
export function toBrowserUrl(u: string | null | undefined): string {
  if (!u) return "";
  return u.replace("://minio:9000/", "://localhost:9000/").split("?")[0];
}

function headerMap(input: HeadersInit | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!input) return out;
  if (typeof Headers !== "undefined" && input instanceof Headers) {
    input.forEach((value, key) => {
      out[key] = value;
    });
    return out;
  }
  if (Array.isArray(input)) {
    input.forEach(([key, value]) => {
      out[key] = value;
    });
    return out;
  }
  Object.entries(input).forEach(([key, value]) => {
    out[key] = value;
  });
  return out;
}

function parseBody<T>(status: number, text: string): T {
  if (status === 204 || text.length === 0) return null as T;
  return JSON.parse(text) as T;
}

function xhrReq<T>(url: string, method: string, headers: Record<string, string>, body: BodyInit | null | undefined): Promise<T> {
  return new Promise((resolve, reject) => {
    const Xhr = globalThis.XMLHttpRequest;
    if (!Xhr) {
      reject(new Error("No browser HTTP transport is available."));
      return;
    }
    const xhr = new Xhr();
    xhr.open(method, url, true);
    Object.entries(headers).forEach(([key, value]) => xhr.setRequestHeader(key, value));
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(xhr.status, xhr.responseText || xhr.statusText));
        return;
      }
      try {
        resolve(parseBody<T>(xhr.status, xhr.responseText || ""));
      } catch (err) {
        reject(err);
      }
    };
    xhr.onerror = () => reject(new Error(`Network request failed: ${method} ${url}`));
    xhr.send((body ?? null) as XMLHttpRequestBodyInit | null);
  });
}

const DEFAULT_TIMEOUT_MS = 15_000;
const AUTH_TIMEOUT_MS = 5_000;

async function req<T>(path: string, opts: RequestInit = {}, timeoutMs?: number): Promise<T> {
  const headers = headerMap(opts.headers);
  const token = auth.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const url = `${BASE}${path}`;
  const ms = timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (typeof globalThis.fetch === "function") {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
      const res = await globalThis.fetch(url, { ...opts, headers, signal: controller.signal });
      if (!res.ok) throw new ApiError(res.status, await res.text().catch(() => res.statusText));
      return parseBody<T>(res.status, await res.text());
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        throw new ApiError(408, `Request timed out after ${ms}ms`);
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
  return xhrReq<T>(url, opts.method ?? "GET", headers, opts.body);
}

/** Shared fetch primitive (seam, owned by Agent 12). Prefixes BASE, attaches the
 *  bearer token, JSON-encodes, throws ApiError on non-2xx, parses JSON (null on
 *  204). Feature modules in src/lib/api/*.ts do `import { http } from "../api"`.
 *  Identical behaviour to the internal `req`; exported under the contract name. */
export const http = req;

// ---- Backend response shapes (subset we consume) ------------------------- //
export interface BookResponse {
  id: string;
  title: string;
  author: string | null;
  status: string;
  num_pages: number | null;
  art_direction: string | null;
  created_at: string | null;
  progress: number | null; // 0..1
  stage: string | null;
}
export interface SourceSpan {
  page?: number;
  para?: number;
  word_range: [number, number]; // [start, end) in the book-global word index
}
export interface ShotResponse {
  shot_id: string;
  status: string;
  render_mode?: string | null;
  duration_s: number | null;
  clip_url: string | null;
  source_span: SourceSpan | null;
  scene_id?: string | null;
  beat_id?: string | null;
  /** This shot's [start, end) offset in seconds within a merged multi-shot
   *  event clip; null for a normal single-shot clip (the common case). */
  clip_start_s?: number | null;
  clip_end_s?: number | null;
}
export interface WordBox {
  word_index: number; // global across the whole book
  text: string;
  bbox: [number, number, number, number]; // normalized [0,1]
}
export interface PageResponse {
  book_id: string;
  page_number: number;
  image_url: string | null;
  text: string | null;
  word_boxes: WordBox[] | null;
}
export interface SessionResponse {
  session_id: string;
  book_id: string;
  focus_word: number;
  velocity_wps: number;
  committed_seconds_ahead: number;
  bursting: boolean;
  budget_remaining_s: number | null;
}
/** SSE payloads we care about (each frame's JSON carries its own `event`). */
export interface BufferState {
  event: "buffer_state";
  committed_seconds_ahead: number;
  bursting: boolean;
  idle: boolean;
  velocity_wps?: number;
  budget_remaining_s: number | null;
}
export interface ClipReady {
  event: "clip_ready";
  shot_id: string;
  oss_url: string;
  video_seconds?: number;
}
export type SessionEvent = BufferState | ClipReady | ({ event: string } & Record<string, unknown>);

interface TokenResponse { access_token: string; token_type: string; expires_in: number }

// Memoize page (cover) fetches so the same book across multiple shelves — or a
// re-mounted HomePage — doesn't re-request page 1. Keyed by `${id}:${n}`; the
// in-flight promise is cached, so concurrent callers share one request. Cleared
// on logout so a new user never sees a previous user's covers.
const pageCache = new Map<string, Promise<PageResponse>>();

export const api = {
  base: BASE,
  isAuthed: () => Boolean(auth.token),
  async login(email: string, password: string): Promise<void> {
    const t = await req<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }, AUTH_TIMEOUT_MS);
    auth.token = t.access_token;
  },
  async register(email: string, password: string): Promise<void> {
    await req("/api/auth/register", { method: "POST", body: JSON.stringify({ email, password }) }, AUTH_TIMEOUT_MS);
  },
  /** Log in, registering first if the account doesn't exist yet. */
  async loginOrRegister(email: string, password: string): Promise<void> {
    try {
      await this.login(email, password);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 404 || e.status === 400)) {
        await this.register(email, password);
        await this.login(email, password);
      } else throw e;
    }
  },
  logout: () => { auth.token = null; pageCache.clear(); },
  listBooks: () => req<BookResponse[]>("/api/books"),
  getBook: (id: string) => req<BookResponse>(`/api/books/${id}`),
  getShots: (id: string) => req<ShotResponse[]>(`/api/books/${id}/shots`),
  getPage: (id: string, n: number) => req<PageResponse>(`/api/books/${id}/pages/${n}`),
  /** Deduplicated/cached `getPage` — for covers fetched repeatedly across shelves. */
  getPageCached(id: string, n: number): Promise<PageResponse> {
    const key = `${id}:${n}`;
    let p = pageCache.get(key);
    if (!p) {
      p = req<PageResponse>(`/api/books/${id}/pages/${n}`).catch((e) => {
        pageCache.delete(key); // don't cache failures — allow a retry
        throw e;
      });
      pageCache.set(key, p);
    }
    return p;
  },
  createSession: (bookId: string, focusWord = 0) =>
    req<SessionResponse>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ book_id: bookId, focus_word: focusWord, mode: "viewer" }),
    }),
  /** Tell the scheduler where the reader is (word index) + how fast (words/sec)
   *  so it generates the window ahead. */
  postIntent: (sessionId: string, focusWord: number, velocity: number, mode?: string) =>
    req<Record<string, unknown>>(`/api/sessions/${sessionId}/intent`, {
      method: "POST",
      body: JSON.stringify({ focus_word: focusWord, velocity, ...(mode ? { mode } : {}) }),
    }),
  /** A jump (fast scroll / page skip) — cancels distant speculative work. */
  seek: (sessionId: string, word: number) =>
    req<Record<string, unknown>>(`/api/sessions/${sessionId}/seek`, {
      method: "POST",
      body: JSON.stringify({ word }),
    }),
  /** Live session stream (clip_ready / buffer_state / …). EventSource can't set
   *  headers, so the JWT rides as ?token=. Returns a close() to unsubscribe. */
  openSessionEvents(sessionId: string, onEvent: (e: SessionEvent) => void): () => void {
    const url = `${BASE}/api/sessions/${sessionId}/events${auth.token ? `?token=${encodeURIComponent(auth.token)}` : ""}`;
    const es = new EventSource(url);
    const handler = (e: MessageEvent) => {
      try {
        onEvent(JSON.parse(e.data) as SessionEvent);
      } catch {
        /* keepalive / non-JSON frame */
      }
    };
    es.onmessage = handler;
    for (const name of ["clip_ready", "buffer_state", "keyframe_ready", "scene_stitched", "agent_activity", "budget_low", "regen_done"]) {
      es.addEventListener(name, handler as EventListener);
    }
    return () => es.close();
  },
  uploadBook(file: File, fields: { title?: string; author?: string; art_direction?: string } = {}) {
    const fd = new FormData();
    fd.append("file", file);
    for (const [k, v] of Object.entries(fields)) if (v) fd.append(k, v);
    return req<BookResponse>("/api/books", { method: "POST", body: fd });
  },
};

// A stable, pleasant cover gradient + spine colour derived from the book id, so
// backend books (which carry no colours) still render as real 3D books.
const PALETTES: Array<{ g: string; spine: string; text: string }> = [
  { g: "linear-gradient(135deg, #1e3a5f 0%, #0d1f33 100%)", spine: "#0a1622", text: "#e8eef5" },
  { g: "linear-gradient(135deg, #6b4226 0%, #3a2414 100%)", spine: "#2a1810", text: "#f3e6d8" },
  { g: "linear-gradient(135deg, #4a5568 0%, #2d3748 100%)", spine: "#1a202c", text: "#e8edf3" },
  { g: "linear-gradient(135deg, #7d2a3a 0%, #4a1622 100%)", spine: "#2e0d15", text: "#f5e3e6" },
  { g: "linear-gradient(135deg, #2d5016 0%, #16300a 100%)", spine: "#0d1f05", text: "#e8f5e9" },
  { g: "linear-gradient(135deg, #b8860b 0%, #6b4e09 100%)", spine: "#3d2c05", text: "#fef6e0" },
];
function paletteFor(id: string) {
  const h = [...id].reduce((a, c) => a + c.charCodeAt(0), 0);
  return PALETTES[h % PALETTES.length];
}

/** Map a backend book onto the renderer's `Book` shape. Cover comes from the
 *  rendered first page; colours are synthesized (the backend has none). */
export function toUiBook(b: BookResponse, coverImage = ""): Book {
  const p = paletteFor(b.id);
  return {
    id: b.id,
    title: b.title,
    author: b.author ?? "Unknown",
    progress: Math.round((b.progress ?? 0) * 100),
    isNew: b.status !== "ready",
    coverColor: p.spine,
    coverGradient: p.g,
    coverImage,
    textColor: p.text,
    spineColor: p.spine,
    live: true,
  };
}
