/**
 * The HTTP transport for the Kinora SDK.
 *
 * Isomorphic: depends only on the standard `fetch`, `AbortController`, and
 * `TextDecoder` available in Node 20+ and browsers. Responsibilities:
 *   - prefix the base URL + `/api`, attach the bearer token,
 *   - JSON-encode bodies (or pass FormData through untouched),
 *   - enforce a per-request timeout via `AbortController`,
 *   - retry idempotent/safe requests on 429/502/503/504 + network errors with
 *     exponential backoff + jitter, honoring `Retry-After`,
 *   - decode the typed error envelope and throw the right {@link KinoraError},
 *   - return both parsed JSON and the raw `Response` (for streaming).
 */
import {
  KinoraError,
  NetworkError,
  RateLimitError,
  TimeoutError,
  errorForStatus,
} from "./errors.js";
import type { ErrorBody } from "./models.js";

/** A function compatible with the standard `fetch`. */
export type FetchLike = (
  input: string | URL,
  init?: RequestInit,
) => Promise<Response>;

/** Retry policy knobs. */
export interface RetryPolicy {
  /** Max attempts (including the first). `1` disables retries. Default 3. */
  maxAttempts: number;
  /** Base backoff in ms (doubled each attempt). Default 250. */
  baseDelayMs: number;
  /** Cap on a single backoff delay in ms. Default 10000. */
  maxDelayMs: number;
  /** Status codes that are retryable. Default [429, 502, 503, 504]. */
  retryStatuses: number[];
}

export const DEFAULT_RETRY: RetryPolicy = {
  maxAttempts: 3,
  baseDelayMs: 250,
  maxDelayMs: 10_000,
  retryStatuses: [429, 502, 503, 504],
};

export interface TransportOptions {
  baseUrl: string;
  apiPrefix: string;
  getToken: () => string | null | undefined;
  fetch: FetchLike;
  timeoutMs: number;
  retry: RetryPolicy;
  /** Extra headers applied to every request (e.g. a User-Agent). */
  defaultHeaders: Record<string, string>;
  /** Optional sleep hook (injected in tests to avoid real timers). */
  sleep?: (ms: number) => Promise<void>;
}

export interface RequestOptions {
  method: string;
  /** Path relative to `apiPrefix`, e.g. `/books/{id}`. May start with `/api`-less. */
  path: string;
  /** JSON body (encoded) or FormData (passed through). */
  body?: unknown;
  /** Query parameters; undefined/null values are dropped. */
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Override the per-request timeout. */
  timeoutMs?: number;
  /** Force this request to retry / not retry (default: retry on GET + known-safe). */
  retryable?: boolean;
  /** Extra per-request headers. */
  headers?: Record<string, string>;
  /** Abort signal supplied by the caller (composed with the timeout signal). */
  signal?: AbortSignal;
}

const RETRY_BY_DEFAULT = new Set(["GET", "HEAD", "DELETE"]);

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Parse a `Retry-After` header (seconds or HTTP-date) into ms. */
export function parseRetryAfter(header: string | null): number | null {
  if (!header) return null;
  const secs = Number(header);
  if (Number.isFinite(secs)) return Math.max(0, secs * 1000);
  const when = Date.parse(header);
  if (Number.isFinite(when)) return Math.max(0, when - Date.now());
  return null;
}

export class Transport {
  constructor(private readonly opts: TransportOptions) {}

  /** Build the absolute URL for a path + query. */
  buildUrl(path: string, query?: RequestOptions["query"]): string {
    const base = this.opts.baseUrl.replace(/\/+$/, "");
    const prefix = this.opts.apiPrefix;
    const rel = path.startsWith("/") ? path : `/${path}`;
    const withPrefix = rel.startsWith(prefix) ? rel : `${prefix}${rel}`;
    const url = new URL(`${base}${withPrefix}`);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
      }
    }
    return url.toString();
  }

  /** Issue a request and parse the JSON response (or null on 204). */
  async request<T>(options: RequestOptions): Promise<T> {
    const res = await this.raw(options);
    const text = await res.text();
    if (res.status === 204 || text.length === 0) return null as T;
    try {
      return JSON.parse(text) as T;
    } catch (cause) {
      throw new KinoraError("failed to parse JSON response", {
        status: res.status,
        body: text,
        request: this.label(options),
        cause,
      });
    }
  }

  /** Issue a request, returning the raw `Response` (for SSE streaming). Throws on non-2xx. */
  async raw(options: RequestOptions): Promise<Response> {
    const url = this.buildUrl(options.path, options.query);
    const retryable = options.retryable ?? RETRY_BY_DEFAULT.has(options.method.toUpperCase());
    const sleep = this.opts.sleep ?? defaultSleep;
    const { maxAttempts, baseDelayMs, maxDelayMs, retryStatuses } = this.opts.retry;
    const attempts = retryable ? maxAttempts : 1;

    let lastError: unknown;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        const res = await this.attempt(url, options);
        if (res.ok) return res;
        // Retryable status?
        if (attempt < attempts && retryStatuses.includes(res.status)) {
          const retryAfter = parseRetryAfter(res.headers.get("retry-after"));
          await sleep(retryAfter ?? this.backoff(attempt, baseDelayMs, maxDelayMs));
          continue;
        }
        throw await this.toError(res, options);
      } catch (err) {
        lastError = err;
        // A thrown KinoraError from a non-retryable status: rethrow immediately.
        if (err instanceof KinoraError && !(err instanceof NetworkError) && !(err instanceof TimeoutError)) {
          throw err;
        }
        // Network/timeout: retry if attempts remain and the request is retryable.
        if (attempt < attempts && retryable) {
          await sleep(this.backoff(attempt, baseDelayMs, maxDelayMs));
          continue;
        }
        throw err;
      }
    }
    // Unreachable in practice; satisfies the type checker.
    throw lastError instanceof Error ? lastError : new NetworkError("request failed");
  }

  private async attempt(url: string, options: RequestOptions): Promise<Response> {
    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? this.opts.timeoutMs;
    // timeoutMs <= 0 disables the idle timer (used for long-lived SSE streams).
    const timer: ReturnType<typeof setTimeout> | undefined =
      timeoutMs > 0
        ? setTimeout(() => controller.abort(new DOMException("timeout", "AbortError")), timeoutMs)
        : undefined;
    // Compose a caller-provided signal with our timeout.
    if (options.signal) {
      if (options.signal.aborted) controller.abort(options.signal.reason);
      else options.signal.addEventListener("abort", () => controller.abort(options.signal?.reason), { once: true });
    }
    const headers = this.headers(options);
    const body = this.encodeBody(options.body, headers);
    try {
      return await this.opts.fetch(url, {
        method: options.method,
        headers,
        body,
        signal: controller.signal,
      });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        const reason = (cause as { message?: string }).message;
        if (reason !== "timeout" && options.signal?.aborted) {
          throw new KinoraError("request aborted by caller", { request: this.label(options), cause });
        }
        throw new TimeoutError(`request timed out after ${timeoutMs}ms`, {
          status: 408,
          request: this.label(options),
          cause,
        });
      }
      throw new NetworkError(`network request failed: ${this.label(options)}`, {
        request: this.label(options),
        cause,
      });
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  private headers(options: RequestOptions): Record<string, string> {
    const out: Record<string, string> = { Accept: "application/json", ...this.opts.defaultHeaders };
    const token = this.opts.getToken();
    if (token) out.Authorization = `Bearer ${token}`;
    if (options.headers) Object.assign(out, options.headers);
    return out;
  }

  private encodeBody(body: unknown, headers: Record<string, string>): BodyInit | undefined {
    if (body === undefined || body === null) return undefined;
    // FormData (uploads): let the runtime set the multipart boundary.
    if (typeof FormData !== "undefined" && body instanceof FormData) return body;
    headers["Content-Type"] = "application/json";
    return JSON.stringify(body);
  }

  private async toError(res: Response, options: RequestOptions): Promise<KinoraError> {
    const raw = await res.text().catch(() => null);
    let envelope: ErrorBody | null = null;
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as { error?: ErrorBody };
        if (parsed && typeof parsed === "object" && parsed.error) envelope = parsed.error;
      } catch {
        /* non-JSON error body */
      }
    }
    const retryAfter = parseRetryAfter(res.headers.get("retry-after"));
    return errorForStatus(res.status, envelope, raw, this.label(options), retryAfter);
  }

  private backoff(attempt: number, base: number, cap: number): number {
    const exp = Math.min(cap, base * 2 ** (attempt - 1));
    // Full jitter in [0, exp].
    return Math.floor(Math.random() * exp);
  }

  private label(options: RequestOptions): string {
    return `${options.method.toUpperCase()} ${options.path}`;
  }
}

export { RateLimitError };
