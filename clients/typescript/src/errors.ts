/**
 * Typed error hierarchy for the Kinora SDK.
 *
 * Every non-2xx response (and transport failure) is surfaced as a subclass of
 * {@link KinoraError}, so callers can `catch (e) { if (e instanceof NotFoundError) ... }`
 * or branch on `e.status` / `e.type`. The backend ships a stable error envelope
 * `{ "error": { type, message, detail? } }` (see `backend/app/api/errors.py`);
 * the SDK maps that onto these classes by status code + type string.
 */
import type { ErrorBody } from "./models.js";

/** Base class for every error raised by the SDK. */
export class KinoraError extends Error {
  /** HTTP status code (0 for transport-level failures). */
  readonly status: number;
  /** Backend error `type` string (e.g. `book_not_found`), or null. */
  readonly type: string | null;
  /** Structured error detail from the backend envelope, if any. */
  readonly detail: Record<string, unknown> | null;
  /** Raw response body text, when available (for debugging). */
  readonly body: string | null;
  /** The request that failed: `"GET /api/books"`. */
  readonly request: string | null;

  constructor(
    message: string,
    opts: {
      status?: number;
      type?: string | null;
      detail?: Record<string, unknown> | null;
      body?: string | null;
      request?: string | null;
      cause?: unknown;
    } = {},
  ) {
    super(message, opts.cause !== undefined ? { cause: opts.cause } : undefined);
    this.name = new.target.name;
    this.status = opts.status ?? 0;
    this.type = opts.type ?? null;
    this.detail = opts.detail ?? null;
    this.body = opts.body ?? null;
    this.request = opts.request ?? null;
    // Maintain a clean prototype chain when targeting older runtimes.
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** 401 — missing/invalid bearer token, or wrong credentials. */
export class AuthError extends KinoraError {}
/** 403 — authenticated but not allowed (e.g. local-only endpoint in prod). */
export class ForbiddenError extends KinoraError {}
/** 404 — resource not found / not owned by the caller. */
export class NotFoundError extends KinoraError {}
/** 409 — a conflict (email taken, etc.). */
export class ConflictError extends KinoraError {}
/** 409 — live video generation is gated off (KINORA_LIVE_VIDEO). */
export class LiveVideoDisabledError extends KinoraError {}
/** 402 — the hard video-second budget cap was reached. */
export class BudgetExceededError extends KinoraError {}
/** 413/415 — upload too large / unsupported media type. */
export class UploadError extends KinoraError {}
/** 422 — request validation failed. `detail.errors` lists the bad fields. */
export class ValidationError extends KinoraError {}
/** 429 — rate limited or quota exceeded. Honor `retryAfterMs`. */
export class RateLimitError extends KinoraError {
  /** Suggested delay before retrying, in ms (from `Retry-After`), if present. */
  readonly retryAfterMs: number | null;
  constructor(message: string, opts: ConstructorParameters<typeof KinoraError>[1] & { retryAfterMs?: number | null } = {}) {
    super(message, opts);
    this.retryAfterMs = opts.retryAfterMs ?? null;
  }
}
/** 502 — an upstream model/provider failure. */
export class ProviderError extends KinoraError {}
/** 5xx — a server error. */
export class ServerError extends KinoraError {}
/** The request timed out (client-side abort). */
export class TimeoutError extends KinoraError {}
/** A network/transport failure (DNS, connection refused, etc.). */
export class NetworkError extends KinoraError {}

/** Map an HTTP status + backend error type onto the right error class. */
export function errorForStatus(
  status: number,
  body: ErrorBody | null,
  raw: string | null,
  request: string | null,
  retryAfterMs: number | null,
): KinoraError {
  const type = body?.type ?? null;
  const message = body?.message ?? `request failed with status ${status}`;
  const detail = (body?.detail as Record<string, unknown> | null | undefined) ?? null;
  const base = { status, type, detail, body: raw, request };

  if (type === "live_video_disabled") return new LiveVideoDisabledError(message, base);
  if (type === "budget_exceeded") return new BudgetExceededError(message, base);
  if (type === "provider_error") return new ProviderError(message, base);

  switch (status) {
    case 401:
      return new AuthError(message, base);
    case 402:
      return new BudgetExceededError(message, base);
    case 403:
      return new ForbiddenError(message, base);
    case 404:
      return new NotFoundError(message, base);
    case 409:
      return new ConflictError(message, base);
    case 413:
    case 415:
      return new UploadError(message, base);
    case 422:
      return new ValidationError(message, base);
    case 429:
      return new RateLimitError(message, { ...base, retryAfterMs });
    case 502:
      return new ProviderError(message, base);
  }
  if (status >= 500) return new ServerError(message, base);
  return new KinoraError(message, base);
}
