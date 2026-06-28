/**
 * @kinora/sdk — typed, isomorphic TypeScript SDK for the Kinora API.
 *
 * ```ts
 * import { KinoraClient } from "@kinora/sdk";
 *
 * const client = new KinoraClient({ baseUrl: "http://localhost:8000" });
 * await client.auth.login({ email: "demo@kinora.local", password: "demo-password-123" });
 * const books = await client.books.list();
 * for (const book of books) console.log(book.title, book.status);
 * ```
 */

export { KinoraClient } from "./client.js";
export type { KinoraClientOptions, StreamOptions, UploadBookFields } from "./client.js";

export {
  MemoryTokenStore,
  browserTokenStore,
} from "./auth.js";
export type { TokenStore } from "./auth.js";

export {
  Transport,
  DEFAULT_RETRY,
  parseRetryAfter,
} from "./transport.js";
export type { FetchLike, RetryPolicy, RequestOptions, TransportOptions } from "./transport.js";

export { Page } from "./pagination.js";

export {
  parseSseStream,
  parseFrame,
  isEvent,
} from "./events.js";
export type {
  SessionEvent,
  KnownEvent,
  KnownEventName,
  EventByName,
  LibraryEvent,
  RawSseFrame,
  BufferStateEvent,
  ClipReadyEvent,
  KeyframeReadyEvent,
  SceneStitchedEvent,
  EventStitchedEvent,
  AgentActivityEvent,
  RegenDoneEvent,
  BudgetLowEvent,
  ConflictChoiceEvent,
  IngestProgressEvent,
  UnknownEvent,
} from "./events.js";

export {
  KinoraError,
  AuthError,
  ForbiddenError,
  NotFoundError,
  ConflictError,
  LiveVideoDisabledError,
  BudgetExceededError,
  UploadError,
  ValidationError,
  RateLimitError,
  ProviderError,
  ServerError,
  TimeoutError,
  NetworkError,
  errorForStatus,
} from "./errors.js";

export * from "./models.js";

// The single source-of-truth spec is re-exported so callers can introspect the
// documented surface (endpoint catalog, events, errors) at runtime. `spec.ts` is
// generated from clients/spec/catalog.mjs by `node clients/spec/sync-ts.mjs`.
export {
  API_VERSION,
  API_PREFIX,
  DEFAULT_BASE_URL,
  ENDPOINTS,
  EVENTS,
  ERROR_TYPES,
  CONFLICT_OPTIONS,
  WEBSOCKET,
  endpointsByTag,
  fullPath,
} from "./spec.js";
export type { EndpointSpec, EventSpec, ErrorTypeSpec, HttpMethod } from "./spec.js";
