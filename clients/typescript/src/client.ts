/**
 * The Kinora API client.
 *
 * `KinoraClient` is the single entrypoint. It owns a {@link Transport} (base URL,
 * token, retries, timeouts) and exposes the API through resource namespaces that
 * mirror the backend route groups:
 *
 *   client.auth      register / login / me / logout
 *   client.books     upload / list / get / page / canon / shots / cover
 *   client.films     events / scene film
 *   client.sessions  create / get / intent / seek / events (SSE)
 *   client.director  comment / canonEdit / conflictChoice / conflicts
 *   client.prefs     get/reset directing style (user + book)
 *   client.eval      buffer trace / report
 *   client.optim     cost / perf
 *
 * Isomorphic: works in Node 20+ and browsers. Construct with at least a
 * `baseUrl`; pass `token` to start authenticated, or call `auth.login()`.
 */
import { browserTokenStore, MemoryTokenStore, type TokenStore } from "./auth.js";
import { DEFAULT_RETRY, Transport, type FetchLike, type RetryPolicy } from "./transport.js";
import { KinoraError } from "./errors.js";
import { Page } from "./pagination.js";
import {
  parseSseStream,
  type LibraryEvent,
  type SessionEvent,
} from "./events.js";
import { API_PREFIX, DEFAULT_BASE_URL } from "./spec.js";
import type {
  BookResponse,
  BufferTracePoint,
  CanonEditRequest,
  CanonEditResponse,
  CanonResponse,
  CommentRequest,
  CommentResponse,
  ConflictChoiceRequest,
  ConflictChoiceResponse,
  ConflictRecordResponse,
  CostReport,
  CreateSessionRequest,
  DirectingStyleResponse,
  EvalReport,
  EventsResponse,
  IntentRequest,
  IntentResponse,
  LoginRequest,
  PageResponse,
  PerfReport,
  RegisterRequest,
  ResetPrefsResponse,
  SceneFilm,
  SeekRequest,
  SeekResponse,
  SessionResponse,
  ShotResponse,
  TokenResponse,
  UserResponse,
} from "./models.js";

export interface KinoraClientOptions {
  /** Backend base URL. Default `http://localhost:8000`. */
  baseUrl?: string;
  /** A starting bearer token (overrides whatever the store holds). */
  token?: string | null;
  /** A custom token store; default is in-memory. Use {@link browserTokenStore} in a browser. */
  tokenStore?: TokenStore;
  /** A `fetch` implementation (injected in tests). Default: global `fetch`. */
  fetch?: FetchLike;
  /** Per-request timeout in ms. Default 15000. */
  timeoutMs?: number;
  /** Retry policy. Default: 3 attempts, exp backoff on 429/502/503/504. */
  retry?: Partial<RetryPolicy>;
  /** Extra default headers (e.g. a custom User-Agent). */
  headers?: Record<string, string>;
  /** Test hook: replace the backoff sleep. */
  sleep?: (ms: number) => Promise<void>;
}

/** Options for streaming SSE events. */
export interface StreamOptions {
  /** Abort the stream. */
  signal?: AbortSignal;
  /**
   * Carry the token as a `?token=` query param too (EventSource parity). The
   * SDK always sends the bearer header, so this is only needed against proxies
   * that strip auth headers from streaming responses. Default false.
   */
  tokenInQuery?: boolean;
}

export class KinoraClient {
  readonly baseUrl: string;
  private readonly transport: Transport;
  private readonly store: TokenStore;

  readonly auth: AuthResource;
  readonly books: BooksResource;
  readonly films: FilmsResource;
  readonly sessions: SessionsResource;
  readonly director: DirectorResource;
  readonly prefs: PrefsResource;
  readonly eval: EvalResource;
  readonly optim: OptimResource;

  constructor(options: KinoraClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.store = options.tokenStore ?? new MemoryTokenStore(options.token ?? null);
    if (options.token !== undefined && options.tokenStore) this.store.set(options.token);
    const fetchImpl: FetchLike | undefined =
      options.fetch ?? (typeof fetch !== "undefined" ? (fetch as FetchLike) : undefined);
    if (!fetchImpl) {
      throw new KinoraError("no `fetch` implementation available; pass options.fetch");
    }
    this.transport = new Transport({
      baseUrl: this.baseUrl,
      apiPrefix: API_PREFIX,
      getToken: () => this.store.get(),
      fetch: fetchImpl,
      timeoutMs: options.timeoutMs ?? 15_000,
      retry: { ...DEFAULT_RETRY, ...options.retry },
      defaultHeaders: { "User-Agent": "kinora-sdk-ts/1.0.0", ...options.headers },
      sleep: options.sleep,
    });

    this.auth = new AuthResource(this.transport, this.store);
    this.books = new BooksResource(this.transport);
    this.films = new FilmsResource(this.transport);
    this.sessions = new SessionsResource(this.transport, this.store);
    this.director = new DirectorResource(this.transport);
    this.prefs = new PrefsResource(this.transport);
    this.eval = new EvalResource(this.transport);
    this.optim = new OptimResource(this.transport);
  }

  /** Whether a bearer token is currently set. */
  isAuthenticated(): boolean {
    return Boolean(this.store.get());
  }

  /** The current bearer token (or null). */
  get token(): string | null {
    return this.store.get() ?? null;
  }

  /** Set/clear the bearer token directly. */
  set token(value: string | null) {
    this.store.set(value);
  }

  /** Internal: the underlying transport (for advanced use / testing). */
  get _transport(): Transport {
    return this.transport;
  }
}

// --------------------------------------------------------------------------- //
// Resource namespaces
// --------------------------------------------------------------------------- //

class AuthResource {
  constructor(
    private readonly t: Transport,
    private readonly store: TokenStore,
  ) {}

  /** Create an account. Does not log in. */
  register(body: RegisterRequest): Promise<UserResponse> {
    return this.t.request<UserResponse>({ method: "POST", path: "/auth/register", body });
  }

  /** Log in and store the bearer token on the client. Returns the token response. */
  async login(body: LoginRequest): Promise<TokenResponse> {
    const token = await this.t.request<TokenResponse>({ method: "POST", path: "/auth/login", body });
    this.store.set(token.access_token);
    return token;
  }

  /** Log in, registering first if the account does not exist (mirrors the renderer). */
  async loginOrRegister(body: LoginRequest & RegisterRequest): Promise<TokenResponse> {
    try {
      return await this.login(body);
    } catch (e) {
      if (e instanceof KinoraError && [400, 401, 404].includes(e.status)) {
        await this.register(body);
        return await this.login(body);
      }
      throw e;
    }
  }

  /** Return the authenticated user. */
  me(): Promise<UserResponse> {
    return this.t.request<UserResponse>({ method: "GET", path: "/auth/me" });
  }

  /** Clear the stored token. */
  logout(): void {
    this.store.set(null);
  }
}

/** Optional metadata for an upload. */
export interface UploadBookFields {
  title?: string;
  author?: string;
  art_direction?: string;
}

class BooksResource {
  constructor(private readonly t: Transport) {}

  /**
   * Upload a PDF/EPUB and trigger Phase-A ingest. `file` may be a `Blob`/`File`
   * (browser) or a `Uint8Array`/`ArrayBuffer` (Node); pass `filename` for the
   * latter so the backend can derive a title.
   */
  async upload(
    file: Blob | Uint8Array | ArrayBuffer,
    fields: UploadBookFields & { filename?: string } = {},
  ): Promise<BookResponse> {
    if (typeof FormData === "undefined") {
      throw new KinoraError("FormData is not available in this runtime; cannot upload");
    }
    const fd = new FormData();
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart], { type: "application/pdf" });
    fd.append("file", blob, fields.filename ?? "book.pdf");
    if (fields.title) fd.append("title", fields.title);
    if (fields.author) fd.append("author", fields.author);
    if (fields.art_direction) fd.append("art_direction", fields.art_direction);
    return this.t.request<BookResponse>({ method: "POST", path: "/books", body: fd });
  }

  /** List the books the current user owns (the shelf), newest first. */
  async list(): Promise<Page<BookResponse>> {
    const items = await this.t.request<BookResponse[]>({ method: "GET", path: "/books" });
    return new Page(items ?? []);
  }

  /** Fetch one book with its import status + progress. */
  get(bookId: string): Promise<BookResponse> {
    return this.t.request<BookResponse>({ method: "GET", path: `/books/${enc(bookId)}` });
  }

  /** A page's presigned image URL, text, and per-word boxes. */
  page(bookId: string, pageNumber: number): Promise<PageResponse> {
    return this.t.request<PageResponse>({
      method: "GET",
      path: `/books/${enc(bookId)}/pages/${pageNumber}`,
    });
  }

  /** The book's canon graph: entities, continuity facts, markdown vault. */
  canon(bookId: string): Promise<CanonResponse> {
    return this.t.request<CanonResponse>({ method: "GET", path: `/books/${enc(bookId)}/canon` });
  }

  /** The book's shots (the shot timeline). */
  async shots(bookId: string): Promise<Page<ShotResponse>> {
    const items = await this.t.request<ShotResponse[]>({
      method: "GET",
      path: `/books/${enc(bookId)}/shots`,
    });
    return new Page(items ?? []);
  }

  /** Poll `get(bookId)` until the book reaches `status: ready` (or a timeout). */
  async waitUntilReady(
    bookId: string,
    opts: { intervalMs?: number; timeoutMs?: number; onProgress?: (b: BookResponse) => void } = {},
  ): Promise<BookResponse> {
    const interval = opts.intervalMs ?? 2000;
    const deadline = Date.now() + (opts.timeoutMs ?? 600_000);
    for (;;) {
      const book = await this.get(bookId);
      opts.onProgress?.(book);
      if (book.status === "ready") return book;
      if (book.status === "failed") {
        throw new KinoraError(`book ${bookId} ingest failed`, { type: "ingest_failed" });
      }
      if (Date.now() >= deadline) {
        throw new KinoraError(`book ${bookId} not ready after timeout`, { type: "timeout" });
      }
      await new Promise((r) => setTimeout(r, interval));
    }
  }
}

class FilmsResource {
  constructor(private readonly t: Transport) {}

  /** Every event (scene) film for a book — stitched URL + sync map + restore state. */
  events(bookId: string): Promise<EventsResponse> {
    return this.t.request<EventsResponse>({ method: "GET", path: `/books/${enc(bookId)}/events` });
  }

  /** One scene's film (partial load). */
  sceneFilm(bookId: string, sceneId: string): Promise<SceneFilm> {
    return this.t.request<SceneFilm>({
      method: "GET",
      path: `/books/${enc(bookId)}/scenes/${enc(sceneId)}/film`,
    });
  }
}

class SessionsResource {
  constructor(
    private readonly t: Transport,
    private readonly store: TokenStore,
  ) {}

  /** Open a reading session against a book. */
  create(body: CreateSessionRequest): Promise<SessionResponse> {
    return this.t.request<SessionResponse>({ method: "POST", path: "/sessions", body });
  }

  /** Return the Scheduler's live control state for a session. */
  get(sessionId: string): Promise<SessionResponse> {
    return this.t.request<SessionResponse>({ method: "GET", path: `/sessions/${enc(sessionId)}` });
  }

  /** Apply a debounced reading-intent update and run one control tick. */
  intent(sessionId: string, body: IntentRequest): Promise<IntentResponse> {
    return this.t.request<IntentResponse>({
      method: "POST",
      path: `/sessions/${enc(sessionId)}/intent`,
      body,
      // Intent updates are idempotent w.r.t. the scheduler tick; safe to retry.
      retryable: true,
    });
  }

  /** Jump to a word: cancel distant work, bridge keyframe, re-seed. */
  seek(sessionId: string, body: SeekRequest): Promise<SeekResponse> {
    return this.t.request<SeekResponse>({
      method: "POST",
      path: `/sessions/${enc(sessionId)}/seek`,
      body,
      retryable: true,
    });
  }

  /**
   * Stream this session's generation events as an async iterator of typed
   * events. Reads the `fetch` response body (isomorphic; no `EventSource`),
   * sending the bearer in the header. Iteration ends when the server closes
   * the stream or the `signal` aborts.
   *
   * ```ts
   * for await (const ev of client.sessions.events(sessionId, { signal })) {
   *   if (ev.event === "clip_ready") play(ev.oss_url);
   * }
   * ```
   */
  async *events(sessionId: string, opts: StreamOptions = {}): AsyncGenerator<SessionEvent> {
    const token = this.store.get();
    const query = opts.tokenInQuery && token ? { token } : undefined;
    const res = await this.t.raw({
      method: "GET",
      path: `/sessions/${enc(sessionId)}/events`,
      query,
      headers: { Accept: "text/event-stream" },
      timeoutMs: 0, // streaming: no idle timeout (Transport treats 0 as "no timer")
      signal: opts.signal,
    });
    if (!res.body) throw new KinoraError("event stream has no body", { request: `GET /sessions/${sessionId}/events` });
    yield* parseSseStream<SessionEvent>(res.body, opts.signal);
  }

  /**
   * Convenience: subscribe with a callback instead of `for await`. Returns an
   * `unsubscribe()` that aborts the stream. Errors go to `onError`.
   */
  subscribe(
    sessionId: string,
    onEvent: (event: SessionEvent) => void,
    opts: StreamOptions & { onError?: (err: unknown) => void; onClose?: () => void } = {},
  ): () => void {
    const controller = new AbortController();
    const signal = opts.signal ? anySignal([opts.signal, controller.signal]) : controller.signal;
    void (async () => {
      try {
        for await (const ev of this.events(sessionId, { ...opts, signal })) onEvent(ev);
        opts.onClose?.();
      } catch (err) {
        if (!controller.signal.aborted) opts.onError?.(err);
      }
    })();
    return () => controller.abort();
  }
}

class DirectorResource {
  constructor(private readonly t: Transport) {}

  /** Classify a Director region-comment, enqueue a regen, emit agent_activity. */
  comment(sessionId: string, body: CommentRequest): Promise<CommentResponse> {
    return this.t.request<CommentResponse>({
      method: "POST",
      path: `/sessions/${enc(sessionId)}/comment`,
      body,
    });
  }

  /** Edit a canon entity and surgically regen only the dependent shots. */
  canonEdit(bookId: string, body: CanonEditRequest): Promise<CanonEditResponse> {
    return this.t.request<CanonEditResponse>({
      method: "POST",
      path: `/books/${enc(bookId)}/canon_edit`,
      body,
    });
  }

  /** Apply the Director's resolution of a surfaced continuity conflict. */
  conflictChoice(sessionId: string, body: ConflictChoiceRequest): Promise<ConflictChoiceResponse> {
    return this.t.request<ConflictChoiceResponse>({
      method: "POST",
      path: `/sessions/${enc(sessionId)}/conflict_choice`,
      body,
    });
  }

  /** The session's conflict log — surfaced disputes + their resolutions. */
  async conflicts(sessionId: string): Promise<Page<ConflictRecordResponse>> {
    const items = await this.t.request<ConflictRecordResponse[]>({
      method: "GET",
      path: `/sessions/${enc(sessionId)}/conflicts`,
    });
    return new Page(items ?? []);
  }

  /** DEV-ONLY (local env): surface the canonical lost-sword demo conflict. */
  demoConflict(sessionId: string): Promise<ConflictRecordResponse> {
    return this.t.request<ConflictRecordResponse>({
      method: "POST",
      path: `/sessions/${enc(sessionId)}/demo/conflict`,
    });
  }
}

class PrefsResource {
  constructor(private readonly t: Transport) {}

  /** The reader's accumulated directing style across all their books. */
  me(): Promise<DirectingStyleResponse> {
    return this.t.request<DirectingStyleResponse>({ method: "GET", path: "/me/prefs" });
  }

  /** The directing style learned for one book. */
  book(bookId: string): Promise<DirectingStyleResponse> {
    return this.t.request<DirectingStyleResponse>({ method: "GET", path: `/books/${enc(bookId)}/prefs` });
  }

  /** Clear the reader's learned directing style everywhere. */
  resetMe(): Promise<ResetPrefsResponse> {
    return this.t.request<ResetPrefsResponse>({ method: "DELETE", path: "/me/prefs" });
  }

  /** Clear the directing style learned for one book. */
  resetBook(bookId: string): Promise<ResetPrefsResponse> {
    return this.t.request<ResetPrefsResponse>({ method: "DELETE", path: `/books/${enc(bookId)}/prefs` });
  }
}

class EvalResource {
  constructor(private readonly t: Transport) {}

  /** Recompute the watermark buffer sawtooth for a session (zero video-seconds). */
  async bufferTrace(
    sessionId: string,
    opts: { velocity?: number; durationS?: number } = {},
  ): Promise<BufferTracePoint[]> {
    const items = await this.t.request<BufferTracePoint[]>({
      method: "GET",
      path: `/eval/buffer-trace/${enc(sessionId)}`,
      query: { velocity: opts.velocity, duration_s: opts.durationS },
    });
    return items ?? [];
  }

  /** The cached crew-vs-baseline evaluation report for a book. */
  report(bookId: string): Promise<EvalReport> {
    return this.t.request<EvalReport>({ method: "GET", path: `/eval/report/${enc(bookId)}` });
  }
}

class OptimResource {
  constructor(private readonly t: Transport) {}

  /** Per book / session / model / operation USD rollup. */
  cost(): Promise<CostReport> {
    return this.t.request<CostReport>({ method: "GET", path: "/optim/cost" });
  }

  /** Compact cost/uptime summary for an in-app HUD. */
  perf(): Promise<PerfReport> {
    return this.t.request<PerfReport>({ method: "GET", path: "/optim/perf" });
  }
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

/** Combine multiple AbortSignals into one (aborts when any aborts). */
function anySignal(signals: AbortSignal[]): AbortSignal {
  const controller = new AbortController();
  for (const s of signals) {
    if (s.aborted) {
      controller.abort(s.reason);
      break;
    }
    s.addEventListener("abort", () => controller.abort(s.reason), { once: true });
  }
  return controller.signal;
}

export type { LibraryEvent, SessionEvent };
