import { getToken } from "./token";
import type {
  Book,
  CanonEditRequest,
  CanonGraph,
  CommentRequest,
  ConflictChoiceRequest,
  CreateSessionResponse,
  Credentials,
  EvalReport,
  IntentUpdate,
  LoginResponse,
  Page,
  Session,
  Shot,
  User,
  BufferTracePoint,
} from "./types";

export const API_BASE = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// The app registers a single handler so a 401 anywhere can clear the session
// and bounce to /login without every call site re-implementing it.
let unauthorizedHandler: (() => void) | null = null;
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Set when sending FormData (multipart) so we don't force a JSON header. */
  raw?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: options.method ?? (options.body !== undefined ? "POST" : "GET"),
      headers,
      body,
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "Network error — is the backend running?", err);
  }

  if (res.status === 401) {
    unauthorizedHandler?.();
    throw new ApiError(401, "Your session has expired. Please sign in again.");
  }

  if (!res.ok) {
    const detail = await safeErrorMessage(res);
    throw new ApiError(res.status, detail, undefined);
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

async function safeErrorMessage(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown; message?: unknown };
    const detail = data.detail ?? data.message;
    if (typeof detail === "string") return detail;
    if (detail) return JSON.stringify(detail);
  } catch {
    // fall through to the status text
  }
  return res.statusText || `Request failed (${res.status})`;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export const auth = {
  register: (creds: Credentials) =>
    request<User>("/auth/register", { body: creds }),
  login: (creds: Credentials) =>
    request<LoginResponse>("/auth/login", { body: creds }),
  me: (signal?: AbortSignal) => request<User>("/auth/me", { signal }),
};

// ---------------------------------------------------------------------------
// Books
// ---------------------------------------------------------------------------
export const books = {
  list: (signal?: AbortSignal) => request<Book[]>("/books", { signal }),
  get: (id: string, signal?: AbortSignal) =>
    request<Book>(`/books/${id}`, { signal }),
  getPage: (id: string, n: number, signal?: AbortSignal) =>
    request<Page>(`/books/${id}/pages/${n}`, { signal }),
  getCanon: (id: string, signal?: AbortSignal) =>
    request<CanonGraph>(`/books/${id}/canon`, { signal }),
  getShots: (id: string, signal?: AbortSignal) =>
    request<Shot[]>(`/books/${id}/shots`, { signal }),
  canonEdit: (id: string, body: CanonEditRequest) =>
    request<CanonEntityEditResponse>(`/books/${id}/canon_edit`, { body }),
  /**
   * Multipart PDF upload with progress. Uses XHR because fetch cannot report
   * upload progress, and the shelf shows a live "preparing…" strip.
   */
  upload: (file: File, onProgress?: (fraction: number) => void) =>
    new Promise<Book>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE}/books`);
      const token = getToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        if (xhr.status === 401) {
          unauthorizedHandler?.();
          reject(new ApiError(401, "Your session has expired."));
          return;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as Book);
          } catch (err) {
            reject(new ApiError(xhr.status, "Malformed upload response", err));
          }
        } else {
          reject(new ApiError(xhr.status, xhr.responseText || "Upload failed"));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "Upload network error"));
      xhr.send(form);
    }),
};

export interface CanonEntityEditResponse {
  version?: number;
  affected_shot_ids?: string[];
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------
export const sessions = {
  create: (bookId: string) =>
    request<CreateSessionResponse>("/sessions", { body: { book_id: bookId } }),
  get: (id: string, signal?: AbortSignal) =>
    request<Session>(`/sessions/${id}`, { signal }),
  intent: (id: string, body: IntentUpdate, signal?: AbortSignal) =>
    request<void>(`/sessions/${id}/intent`, { body, signal }),
  seek: (id: string, word: number) =>
    request<void>(`/sessions/${id}/seek`, { body: { word } }),
  comment: (id: string, body: CommentRequest) =>
    request<void>(`/sessions/${id}/comment`, { body }),
  conflictChoice: (id: string, body: ConflictChoiceRequest) =>
    request<void>(`/sessions/${id}/conflict_choice`, { body }),
};

// ---------------------------------------------------------------------------
// Eval / metrics
// ---------------------------------------------------------------------------
export const evalApi = {
  bufferTrace: (sessionId: string, signal?: AbortSignal) =>
    request<BufferTracePoint[]>(`/eval/buffer-trace/${sessionId}`, { signal }),
  report: (bookId: string, signal?: AbortSignal) =>
    request<EvalReport>(`/eval/report/${bookId}`, { signal }),
};

// ---------------------------------------------------------------------------
// Realtime transport URLs (auth via ?token= since EventSource/WS can't set
// Authorization headers).
// ---------------------------------------------------------------------------
export function eventStreamUrl(sessionId: string): string {
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${API_BASE}/sessions/${sessionId}/events${q}`;
}

export function libraryEventsUrl(): string {
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${API_BASE}/books/events${q}`;
}

export function websocketUrl(sessionId: string): string {
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${API_BASE}/ws/sessions/${sessionId}${q}`;
}
