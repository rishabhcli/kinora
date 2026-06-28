// Deterministic network mock for the Kinora FastAPI backend.
//
// Installs Playwright route handlers over the API base (default
// http://localhost:8000, overridable with VITE_KINORA_API_URL) so the renderer's
// `apps/desktop/src/lib/api.ts` client sees a complete, reproducible backend with
// NO real network, NO Docker stack, and NO live Wan credits. The SSE
// `/events` endpoint is faked too — EventSource is monkeypatched in the page
// context (Playwright can't fulfil a never-ending text/event-stream cleanly), so
// `clip_ready` / `buffer_state` frames can be scripted on demand.
//
// Usage (see e2e/fixtures/test.ts which wires this as a fixture):
//   const mock = new ApiMock(page);
//   await mock.install();                 // route handlers + EventSource shim
//   await mock.pushEvent({ event: "buffer_state", ... }); // optional live frames

import type { Page, Route, Request } from "@playwright/test";
import {
  SEED_BOOKS,
  SEED_PAGES,
  SEED_SHOTS,
  FAKE_TOKEN,
  type SeedBook,
} from "../fixtures/seed";

const DEFAULT_BASE = "http://localhost:8000";

export interface ApiMockOptions {
  /** API base the renderer calls (mirror of VITE_KINORA_API_URL). */
  base?: string;
  /** Override the seed library; defaults to SEED_BOOKS. */
  books?: SeedBook[];
  /** Force auth endpoints to fail (login/register 401) to exercise demo fallback. */
  authFails?: boolean;
  /** Simulate the whole backend being unreachable (every call aborts). */
  offline?: boolean;
  /** Artificial latency (ms) added to each response — for perf / spinner tests. */
  latencyMs?: number;
}

interface MockState {
  uploads: SeedBook[];
}

export class ApiMock {
  readonly base: string;
  private books: SeedBook[];
  private readonly opts: ApiMockOptions;
  private readonly state: MockState = { uploads: [] };

  constructor(
    private readonly page: Page,
    opts: ApiMockOptions = {},
  ) {
    this.opts = opts;
    this.base = (opts.base ?? DEFAULT_BASE).replace(/\/$/, "");
    this.books = [...(opts.books ?? SEED_BOOKS)];
  }

  /** All books currently visible (seed + anything "uploaded" during the test). */
  get library(): SeedBook[] {
    return [...this.books, ...this.state.uploads];
  }

  /** Install the EventSource shim + the route handlers. Call once per page. */
  async install(): Promise<void> {
    await this.installEventSourceShim();
    await this.page.route(`${this.base}/api/**`, (route, request) =>
      this.handle(route, request),
    );
  }

  /**
   * Push an SSE frame to any open session stream in the page. The shim exposes
   * `window.__kinoraPushEvent` which dispatches both an onmessage and a named
   * event so the api.ts listeners (clip_ready/buffer_state/…) fire.
   */
  async pushEvent(event: Record<string, unknown>): Promise<void> {
    await this.page.evaluate((e) => {
      (window as unknown as { __kinoraPushEvent?: (x: unknown) => void }).__kinoraPushEvent?.(e);
    }, event);
  }

  // ---- internals -------------------------------------------------------- //

  private async maybeDelay(): Promise<void> {
    if (this.opts.latencyMs && this.opts.latencyMs > 0) {
      await new Promise((r) => setTimeout(r, this.opts.latencyMs));
    }
  }

  private async handle(route: Route, request: Request): Promise<void> {
    if (this.opts.offline) {
      await route.abort("connectionrefused");
      return;
    }
    await this.maybeDelay();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    // ---- auth ---- //
    if (path === "/api/auth/login" && method === "POST") {
      if (this.opts.authFails) {
        return this.json(route, 401, { detail: "Invalid credentials" });
      }
      return this.json(route, 200, {
        access_token: FAKE_TOKEN,
        token_type: "bearer",
        expires_in: 3600,
      });
    }
    if (path === "/api/auth/register" && method === "POST") {
      if (this.opts.authFails) return this.json(route, 400, { detail: "register disabled" });
      return this.json(route, 201, { ok: true });
    }

    // ---- books ---- //
    if (path === "/api/books" && method === "GET") {
      return this.json(route, 200, this.library);
    }
    if (path === "/api/books" && method === "POST") {
      // Upload: synthesize an importing book and return it.
      const created: SeedBook = {
        id: `upload-${this.state.uploads.length + 1}`,
        title: "Uploaded Book",
        author: "You",
        status: "importing",
        num_pages: null,
        art_direction: null,
        created_at: new Date().toISOString(),
        progress: 0,
        stage: "queued",
      };
      this.state.uploads.push(created);
      return this.json(route, 201, created);
    }

    const bookMatch = path.match(/^\/api\/books\/([^/]+)$/);
    if (bookMatch && method === "GET") {
      const b = this.library.find((x) => x.id === bookMatch[1]);
      return b ? this.json(route, 200, b) : this.json(route, 404, { detail: "not found" });
    }

    const shotsMatch = path.match(/^\/api\/books\/([^/]+)\/shots$/);
    if (shotsMatch && method === "GET") {
      return this.json(route, 200, SEED_SHOTS[shotsMatch[1]] ?? []);
    }

    // ---- director surfaces ---- //
    // Return the EMPTY canon shape — CanonVault reads nested per-entity fields
    // (e.g. aliases.length) that the trimmed BookResponse-style seed wouldn't
    // carry, and a partial entity crashes the component. An empty graph is the
    // same shape the renderer's own graceful-degrade path produces on a 404.
    const canonMatch = path.match(/^\/api\/books\/([^/]+)\/canon$/);
    if (canonMatch && method === "GET") {
      return this.json(route, 200, {
        book_id: canonMatch[1],
        entities: [],
        states: [],
        markdown: null,
      });
    }
    if (/^\/api\/books\/[^/]+\/prefs$/.test(path) && method === "GET") {
      return this.json(route, 200, { book_id: "seed-frog-king", learned: [], defaults: {} });
    }
    if (path === "/api/me/prefs" && method === "GET") {
      return this.json(route, 200, { learned: [], defaults: {} });
    }
    if (/^\/api\/sessions\/[^/]+\/conflicts$/.test(path) && method === "GET") {
      return this.json(route, 200, []);
    }

    const pageMatch = path.match(/^\/api\/books\/([^/]+)\/pages\/(\d+)$/);
    if (pageMatch && method === "GET") {
      const pages = SEED_PAGES[pageMatch[1]] ?? [];
      const n = Number(pageMatch[2]);
      const p = pages.find((x) => x.page_number === n);
      return p
        ? this.json(route, 200, p)
        : this.json(route, 404, { detail: "no such page" });
    }

    // ---- sessions ---- //
    if (path === "/api/sessions" && method === "POST") {
      return this.json(route, 200, {
        session_id: "sess-e2e-1",
        book_id: "seed-frog-king",
        focus_word: 0,
        velocity_wps: 2,
        committed_seconds_ahead: 0,
        bursting: false,
        budget_remaining_s: 0, // KINORA_LIVE_VIDEO off: budget stays 0
      });
    }
    if (/^\/api\/sessions\/[^/]+\/(intent|seek|comment)$/.test(path) && method === "POST") {
      return this.json(route, 200, { ok: true });
    }
    // The SSE stream is handled by the EventSource shim, but guard the route too.
    if (/^\/api\/sessions\/[^/]+\/events/.test(path)) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ": e2e mock stream — frames pushed via window.__kinoraPushEvent\n\n",
      });
    }

    // Unknown API path — surface it loudly so specs catch contract drift.
    return this.json(route, 404, { detail: `e2e mock: unhandled ${method} ${path}` });
  }

  private json(route: Route, status: number, body: unknown): Promise<void> {
    return route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  }

  /**
   * Replace EventSource with a fake that registers itself globally and exposes
   * `window.__kinoraPushEvent`. The real api.ts attaches onmessage + named
   * listeners; the shim fans a pushed frame to both, exactly like a server.
   */
  private async installEventSourceShim(): Promise<void> {
    await this.page.addInitScript(() => {
      type Listener = (e: MessageEvent) => void;
      const open: FakeEventSource[] = [];

      class FakeEventSource {
        url: string;
        readyState = 1;
        onmessage: Listener | null = null;
        onerror: Listener | null = null;
        onopen: Listener | null = null;
        private named: Record<string, Listener[]> = {};

        constructor(url: string) {
          this.url = url;
          open.push(this);
          // Fire open asynchronously like a real connection.
          setTimeout(() => this.onopen?.(new MessageEvent("open")), 0);
        }
        addEventListener(type: string, cb: Listener) {
          (this.named[type] ||= []).push(cb);
        }
        removeEventListener(type: string, cb: Listener) {
          this.named[type] = (this.named[type] || []).filter((h) => h !== cb);
        }
        close() {
          this.readyState = 2;
          const i = open.indexOf(this);
          if (i >= 0) open.splice(i, 1);
        }
        _deliver(payload: unknown) {
          const data = JSON.stringify(payload);
          const evt = new MessageEvent("message", { data });
          this.onmessage?.(evt);
          const name = (payload as { event?: string })?.event;
          if (name && this.named[name]) {
            for (const cb of this.named[name]) cb(new MessageEvent(name, { data }));
          }
        }
      }

      (window as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
      (window as unknown as { __kinoraPushEvent: (p: unknown) => void }).__kinoraPushEvent = (
        payload: unknown,
      ) => {
        for (const es of open) es._deliver(payload);
      };
      (window as unknown as { __kinoraOpenStreams: () => number }).__kinoraOpenStreams = () =>
        open.length;
    });
  }
}
