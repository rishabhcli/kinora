import { describe, it, expect } from "vitest";
import { KinoraClient } from "../src/client.js";
import { AuthError, NotFoundError } from "../src/errors.js";
import { MockFetch, noSleep } from "./helpers.js";

function client(mock: MockFetch, token: string | null = null): KinoraClient {
  return new KinoraClient({ baseUrl: "http://localhost:8000", token, fetch: mock.fetch, sleep: noSleep });
}

describe("auth resource", () => {
  it("login stores the token and authenticates subsequent calls", async () => {
    const mock = new MockFetch()
      .enqueue({ json: { access_token: "abc", token_type: "bearer", expires_in: 3600 } })
      .enqueue({ json: { id: "u1", email: "a@b.co", created_at: null } });
    const c = client(mock);
    expect(c.isAuthenticated()).toBe(false);
    const tok = await c.auth.login({ email: "a@b.co", password: "password1" });
    expect(tok.access_token).toBe("abc");
    expect(c.isAuthenticated()).toBe(true);
    await c.auth.me();
    expect(mock.last()!.headers.Authorization).toBe("Bearer abc");
  });

  it("loginOrRegister registers after a 401 then logs in", async () => {
    const mock = new MockFetch()
      .enqueue({ status: 401, json: { error: { type: "invalid_credentials", message: "no" } } })
      .enqueue({ status: 201, json: { id: "u1", email: "a@b.co", created_at: null } })
      .enqueue({ json: { access_token: "abc", token_type: "bearer", expires_in: 3600 } });
    const c = client(mock);
    const tok = await c.auth.loginOrRegister({ email: "a@b.co", password: "password1" });
    expect(tok.access_token).toBe("abc");
    expect(mock.requests.map((r) => r.method)).toEqual(["POST", "POST", "POST"]);
    expect(mock.requests.map((r) => r.url)).toEqual([
      "http://localhost:8000/api/auth/login",
      "http://localhost:8000/api/auth/register",
      "http://localhost:8000/api/auth/login",
    ]);
  });

  it("loginOrRegister rethrows non-auth errors", async () => {
    const mock = new MockFetch().enqueue({ status: 500, json: { error: { type: "internal_error", message: "x" } } });
    const c = client(mock);
    await expect(c.auth.loginOrRegister({ email: "a@b.co", password: "password1" })).rejects.toThrow();
  });

  it("logout clears the token", () => {
    const c = client(new MockFetch(), "abc");
    expect(c.isAuthenticated()).toBe(true);
    c.auth.logout();
    expect(c.isAuthenticated()).toBe(false);
  });
});

describe("books resource", () => {
  it("list wraps the bare array in a Page", async () => {
    const mock = new MockFetch().enqueue({
      json: [
        { id: "b1", title: "A", status: "ready" },
        { id: "b2", title: "B", status: "importing" },
      ],
    });
    const c = client(mock, "tok");
    const page = await c.books.list();
    expect(page.length).toBe(2);
    expect(page.first()!.title).toBe("A");
    const titles: string[] = [];
    for await (const b of page) titles.push(b.title);
    expect(titles).toEqual(["A", "B"]);
  });

  it("get encodes the book id into the path", async () => {
    const mock = new MockFetch().enqueue({ json: { id: "b/1", title: "X", status: "ready" } });
    const c = client(mock, "tok");
    await c.books.get("b/1");
    expect(mock.last()!.url).toBe("http://localhost:8000/api/books/b%2F1");
  });

  it("page hits the right path", async () => {
    const mock = new MockFetch().enqueue({ json: { book_id: "b1", page_number: 3, word_boxes: [] } });
    const c = client(mock, "tok");
    const p = await c.books.page("b1", 3);
    expect(p.page_number).toBe(3);
    expect(mock.last()!.url).toBe("http://localhost:8000/api/books/b1/pages/3");
  });

  it("shots wraps the bare array in a Page", async () => {
    const mock = new MockFetch().enqueue({ json: [{ shot_id: "s1", status: "accepted" }] });
    const c = client(mock, "tok");
    const shots = await c.books.shots("b1");
    expect(shots.length).toBe(1);
  });

  it("upload posts multipart form data", async () => {
    const mock = new MockFetch().enqueue({ status: 201, json: { id: "b9", title: "My Book", status: "importing" } });
    const c = client(mock, "tok");
    const blob = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: "application/pdf" });
    const book = await c.books.upload(blob, { title: "My Book", filename: "my.pdf" });
    expect(book.id).toBe("b9");
    expect(mock.last()!.method).toBe("POST");
    expect(mock.last()!.url).toBe("http://localhost:8000/api/books");
  });

  it("waitUntilReady polls until ready", async () => {
    const mock = new MockFetch()
      .enqueue({ json: { id: "b1", title: "A", status: "importing", progress: 0.5 } })
      .enqueue({ json: { id: "b1", title: "A", status: "ready", progress: 1 } });
    const c = client(mock, "tok");
    const seen: string[] = [];
    const book = await c.books.waitUntilReady("b1", { intervalMs: 1, onProgress: (b) => seen.push(b.status) });
    expect(book.status).toBe("ready");
    expect(seen).toEqual(["importing", "ready"]);
  });

  it("waitUntilReady throws on failed ingest", async () => {
    const mock = new MockFetch().enqueue({ json: { id: "b1", title: "A", status: "failed" } });
    const c = client(mock, "tok");
    await expect(c.books.waitUntilReady("b1", { intervalMs: 1 })).rejects.toThrow(/ingest failed/);
  });
});

describe("sessions / director / prefs / eval / optim resources", () => {
  it("createSession posts the body", async () => {
    const mock = new MockFetch().enqueue({
      status: 201,
      json: { session_id: "s1", book_id: "b1", focus_word: 0, velocity_wps: 4, mode: "viewer", committed_seconds_ahead: 0, bursting: false, budget_remaining_s: null, inflight: {} },
    });
    const c = client(mock, "tok");
    const s = await c.sessions.create({ book_id: "b1", focus_word: 0, mode: "viewer" });
    expect(s.session_id).toBe("s1");
    expect(mock.last()!.body).toEqual({ book_id: "b1", focus_word: 0, mode: "viewer" });
  });

  it("intent + seek are retryable POSTs", async () => {
    const mock = new MockFetch()
      .enqueue({ status: 503, json: { error: { type: "x", message: "down" } } })
      .enqueue({ json: { session_id: "s1", settled: true, allow_promotion: true, idle: false, bursting: false, committed_seconds_ahead: 30, promoted: [], keyframed: [], cancelled: 0 } });
    const c = client(mock, "tok");
    const r = await c.sessions.intent("s1", { focus_word: 100, velocity: 5 });
    expect(r.committed_seconds_ahead).toBe(30);
    expect(mock.requests.length).toBe(2); // retried
  });

  it("director.canonEdit posts to the book canon_edit path", async () => {
    const mock = new MockFetch().enqueue({ json: { entity_key: "hero", version: 2, affected_shot_ids: ["s1"], skipped_shots: 4 } });
    const c = client(mock, "tok");
    const r = await c.director.canonEdit("b1", { entity_key: "hero", changes: { name: "Jane" } });
    expect(r.version).toBe(2);
    expect(mock.last()!.url).toBe("http://localhost:8000/api/books/b1/canon_edit");
  });

  it("director.conflictChoice posts the option", async () => {
    const mock = new MockFetch().enqueue({ json: { conflict_id: "cf_s1", option: "honor_canon", status: "applied", shot_id: "s1", reasoning: "ok" } });
    const c = client(mock, "tok");
    const r = await c.director.conflictChoice("s1", { conflict_id: "cf_s1", option: "honor_canon" });
    expect(r.status).toBe("applied");
  });

  it("prefs.resetMe issues a DELETE", async () => {
    const mock = new MockFetch().enqueue({ json: { scope: "user", book_id: null, cleared: 3 } });
    const c = client(mock, "tok");
    const r = await c.prefs.resetMe();
    expect(r.cleared).toBe(3);
    expect(mock.last()!.method).toBe("DELETE");
  });

  it("eval.bufferTrace passes query params", async () => {
    const mock = new MockFetch().enqueue({ json: [{ t: 0, committed_seconds_ahead: 25, low: 25, high: 75 }] });
    const c = client(mock, "tok");
    const pts = await c.eval.bufferTrace("s1", { velocity: 5, durationS: 120 });
    expect(pts.length).toBe(1);
    expect(mock.last()!.url).toBe("http://localhost:8000/api/eval/buffer-trace/s1?velocity=5&duration_s=120");
  });

  it("optim.cost / perf hit their paths", async () => {
    const mock = new MockFetch().enqueue({ json: { rollup: {} } }).enqueue({ json: { uptime_s: 1 } });
    const c = client(mock, "tok");
    await c.optim.cost();
    expect(mock.last()!.url).toBe("http://localhost:8000/api/optim/cost");
    await c.optim.perf();
    expect(mock.last()!.url).toBe("http://localhost:8000/api/optim/perf");
  });
});

describe("error propagation through the client", () => {
  it("surfaces 401 as AuthError", async () => {
    const mock = new MockFetch().enqueue({ status: 401, json: { error: { type: "unauthorized", message: "no" } } });
    const c = client(mock, "tok");
    await expect(c.auth.me()).rejects.toBeInstanceOf(AuthError);
  });

  it("surfaces 404 as NotFoundError", async () => {
    const mock = new MockFetch().enqueue({ status: 404, json: { error: { type: "book_not_found", message: "no" } } });
    const c = client(mock, "tok");
    await expect(c.books.get("nope")).rejects.toBeInstanceOf(NotFoundError);
  });
});

describe("constructor", () => {
  it("throws when no fetch is available and none is injected", () => {
    const g = globalThis as { fetch?: unknown };
    const original = g.fetch;
    delete g.fetch; // simulate a runtime without a global fetch
    try {
      expect(() => new KinoraClient({ baseUrl: "http://x" })).toThrow(/no .fetch/);
    } finally {
      (globalThis as { fetch?: unknown }).fetch = original;
    }
  });
});
