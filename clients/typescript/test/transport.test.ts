import { describe, it, expect } from "vitest";
import { Transport, DEFAULT_RETRY, parseRetryAfter } from "../src/transport.js";
import {
  AuthError,
  BudgetExceededError,
  ConflictError,
  ForbiddenError,
  KinoraError,
  LiveVideoDisabledError,
  NetworkError,
  NotFoundError,
  ProviderError,
  RateLimitError,
  ServerError,
  UploadError,
  ValidationError,
} from "../src/errors.js";
import { MockFetch, noSleep } from "./helpers.js";

function makeTransport(mock: MockFetch) {
  return new Transport({
    baseUrl: "http://localhost:8000",
    apiPrefix: "/api",
    getToken: () => "tok-123",
    fetch: mock.fetch,
    timeoutMs: 1000,
    retry: { ...DEFAULT_RETRY },
    defaultHeaders: { "User-Agent": "test" },
    sleep: noSleep,
  });
}

describe("Transport URL building", () => {
  it("prepends base + api prefix and encodes query", () => {
    const t = makeTransport(new MockFetch());
    expect(t.buildUrl("/books")).toBe("http://localhost:8000/api/books");
    expect(t.buildUrl("/books/abc/shots")).toBe("http://localhost:8000/api/books/abc/shots");
    expect(t.buildUrl("/eval/buffer-trace/s1", { velocity: 5, duration_s: 60 })).toBe(
      "http://localhost:8000/api/eval/buffer-trace/s1?velocity=5&duration_s=60",
    );
  });

  it("does not double-prefix paths that already include /api", () => {
    const t = makeTransport(new MockFetch());
    expect(t.buildUrl("/api/books")).toBe("http://localhost:8000/api/books");
  });

  it("drops undefined/null query values", () => {
    const t = makeTransport(new MockFetch());
    expect(t.buildUrl("/x", { a: undefined, b: null, c: "v" })).toBe("http://localhost:8000/api/x?c=v");
  });
});

describe("Transport request", () => {
  it("attaches the bearer token and default headers", async () => {
    const mock = new MockFetch().enqueue({ json: { ok: true } });
    const t = makeTransport(mock);
    await t.request({ method: "GET", path: "/auth/me" });
    expect(mock.last()!.headers.Authorization).toBe("Bearer tok-123");
    expect(mock.last()!.headers["User-Agent"]).toBe("test");
  });

  it("JSON-encodes a body and sets content-type", async () => {
    const mock = new MockFetch().enqueue({ status: 201, json: { id: "u1" } });
    const t = makeTransport(mock);
    const out = await t.request<{ id: string }>({ method: "POST", path: "/auth/register", body: { email: "a@b.co" } });
    expect(out.id).toBe("u1");
    expect(mock.last()!.headers["Content-Type"]).toBe("application/json");
    expect(mock.last()!.body).toEqual({ email: "a@b.co" });
  });

  it("returns null on 204 / empty body", async () => {
    const mock = new MockFetch().enqueue({ status: 204 });
    const t = makeTransport(mock);
    const out = await t.request({ method: "DELETE", path: "/me/prefs" });
    expect(out).toBeNull();
  });

  it("throws KinoraError on unparseable JSON", async () => {
    const mock = new MockFetch().enqueue({ status: 200, text: "<<not json>>" });
    const t = makeTransport(mock);
    await expect(t.request({ method: "GET", path: "/x" })).rejects.toBeInstanceOf(KinoraError);
  });
});

describe("Transport error mapping", () => {
  const cases: Array<[number, string | undefined, unknown]> = [
    [401, "invalid_credentials", AuthError],
    [403, "forbidden", ForbiddenError],
    [404, "book_not_found", NotFoundError],
    [409, "email_taken", ConflictError],
    [409, "live_video_disabled", LiveVideoDisabledError],
    [402, "budget_exceeded", BudgetExceededError],
    [413, "file_too_large", UploadError],
    [415, "unsupported_media_type", UploadError],
    [422, "validation_error", ValidationError],
    [429, "book_quota_exceeded", RateLimitError],
    [502, "provider_error", ProviderError],
    [500, "internal_error", ServerError],
  ];
  for (const [status, type, cls] of cases) {
    it(`maps ${status}/${type} to ${(cls as { name: string }).name}`, async () => {
      // Use `.default` so retryable statuses (429/502/503/504) return the same
      // typed error on every attempt and surface it after exhaustion, rather
      // than draining a single-item queue and hitting an empty-queue throw.
      const mock = new MockFetch().default({
        status,
        json: { error: { type, message: "boom", detail: { k: 1 } } },
      });
      const t = makeTransport(mock);
      const err = await t.request({ method: "GET", path: "/x" }).catch((e) => e);
      expect(err).toBeInstanceOf(cls as never);
      expect((err as KinoraError).status).toBe(status);
      expect((err as KinoraError).type).toBe(type);
      expect((err as KinoraError).detail).toEqual({ k: 1 });
      expect((err as KinoraError).request).toBe("GET /x");
    });
  }

  it("falls back gracefully when the body is not the error envelope", async () => {
    const mock = new MockFetch().enqueue({ status: 500, text: "raw error" });
    const t = makeTransport(mock);
    const err = await t.request({ method: "GET", path: "/x" }).catch((e) => e);
    expect(err).toBeInstanceOf(ServerError);
    expect((err as ServerError).body).toBe("raw error");
  });
});

describe("Transport retries", () => {
  it("retries a 503 on GET and eventually succeeds", async () => {
    const mock = new MockFetch()
      .enqueue({ status: 503, json: { error: { type: "x", message: "down" } } })
      .enqueue({ status: 503, json: { error: { type: "x", message: "down" } } })
      .enqueue({ json: { ok: true } });
    const t = makeTransport(mock);
    const out = await t.request<{ ok: boolean }>({ method: "GET", path: "/x" });
    expect(out.ok).toBe(true);
    expect(mock.requests.length).toBe(3);
  });

  it("does NOT retry a POST by default", async () => {
    const mock = new MockFetch().enqueue({ status: 503, json: { error: { type: "x", message: "down" } } });
    const t = makeTransport(mock);
    await expect(t.request({ method: "POST", path: "/x", body: {} })).rejects.toBeInstanceOf(ServerError);
    expect(mock.requests.length).toBe(1);
  });

  it("retries a POST when retryable:true", async () => {
    const mock = new MockFetch()
      .enqueue({ status: 502, json: { error: { type: "provider_error", message: "x" } } })
      .enqueue({ json: { ok: true } });
    const t = makeTransport(mock);
    const out = await t.request<{ ok: boolean }>({ method: "POST", path: "/x", body: {}, retryable: true });
    expect(out.ok).toBe(true);
    expect(mock.requests.length).toBe(2);
  });

  it("honors Retry-After and stops after maxAttempts", async () => {
    const mock = new MockFetch().default({ status: 429, json: { error: { type: "rate", message: "slow" } }, headers: { "retry-after": "2" } });
    const t = makeTransport(mock);
    const err = await t.request({ method: "GET", path: "/x" }).catch((e) => e);
    expect(err).toBeInstanceOf(RateLimitError);
    expect((err as RateLimitError).retryAfterMs).toBe(2000);
    expect(mock.requests.length).toBe(DEFAULT_RETRY.maxAttempts);
  });

  it("retries on a network error then surfaces it after exhaustion", async () => {
    const mock = new MockFetch().default({ throw: new TypeError("fetch failed") });
    const t = makeTransport(mock);
    const err = await t.request({ method: "GET", path: "/x" }).catch((e) => e);
    expect(err).toBeInstanceOf(NetworkError);
    expect(mock.requests.length).toBe(DEFAULT_RETRY.maxAttempts);
  });
});

describe("parseRetryAfter", () => {
  it("parses seconds", () => {
    expect(parseRetryAfter("5")).toBe(5000);
  });
  it("parses an HTTP date in the future", () => {
    const future = new Date(Date.now() + 3000).toUTCString();
    const ms = parseRetryAfter(future);
    expect(ms).not.toBeNull();
    expect(ms!).toBeGreaterThan(1000);
  });
  it("returns null for garbage / missing", () => {
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
  });
});
