import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ClipCache } from "./clipCache";

// Drain all queued microtasks (the fetch→blob→store promise chain). A single
// macrotask hop reliably flushes the microtask queue that the chain enqueues.
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

// A fake fetch that returns a tiny blob and lets us count requests per URL, so we
// can prove the cache fetches each clip at most once and serves replays from
// memory (no network round-trip on scroll-back).
function makeFetch() {
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (url: string) => {
    calls.push(url);
    return {
      ok: true,
      status: 200,
      blob: async () => new Blob([new Uint8Array([1, 2, 3])], { type: "video/mp4" }),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

describe("ClipCache", () => {
  let created: string[];
  let revoked: string[];
  let n: number;

  beforeEach(() => {
    created = [];
    revoked = [];
    n = 0;
    // Stub object-URL plumbing (jsdom doesn't implement it) so we can observe
    // blob URLs being minted and revoked.
    globalThis.URL.createObjectURL = vi.fn(() => {
      const u = `blob:mock-${++n}`;
      created.push(u);
      return u;
    });
    globalThis.URL.revokeObjectURL = vi.fn((u: string) => {
      revoked.push(u);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches a clip once and replays from the cached blob URL", async () => {
    const { fetchImpl, calls } = makeFetch();
    const cache = new ClipCache(8, fetchImpl);

    // First visit: not cached yet → returns the source URL and warms in background.
    expect(cache.resolve("https://oss/a.mp4")).toBe("https://oss/a.mp4");
    await flush();

    expect(cache.has("https://oss/a.mp4")).toBe(true);
    const blob = cache.resolve("https://oss/a.mp4");
    expect(blob).toMatch(/^blob:/);

    // Scroll-back: resolving again returns the SAME blob URL, no extra fetch.
    expect(cache.resolve("https://oss/a.mp4")).toBe(blob);
    expect(calls).toEqual(["https://oss/a.mp4"]);
  });

  it("dedupes concurrent prefetch + resolve into a single fetch", async () => {
    const { fetchImpl, calls } = makeFetch();
    const cache = new ClipCache(8, fetchImpl);
    cache.prefetch("https://oss/x.mp4");
    cache.resolve("https://oss/x.mp4");
    cache.prefetch("https://oss/x.mp4");
    await flush();
    expect(calls.filter((u) => u === "https://oss/x.mp4").length).toBe(1);
  });

  it("evicts the least-recently-used clip and revokes its blob URL", async () => {
    const { fetchImpl } = makeFetch();
    const cache = new ClipCache(2, fetchImpl); // cap of 2

    const warm = async (u: string) => {
      cache.prefetch(u);
      await flush();
    };

    await warm("a");
    await warm("b");
    const blobA = cache.resolve("a"); // touch a → b is now the LRU
    expect(blobA).toMatch(/^blob:/);
    await warm("c"); // overflow → evict b (the LRU), revoke its blob

    expect(cache.has("a")).toBe(true);
    expect(cache.has("c")).toBe(true);
    expect(cache.has("b")).toBe(false);
    expect(revoked.length).toBe(1);
  });

  it("passes blob: URLs through and never re-fetches them", () => {
    const { fetchImpl, calls } = makeFetch();
    const cache = new ClipCache(8, fetchImpl);
    expect(cache.resolve("blob:already-cached")).toBe("blob:already-cached");
    cache.prefetch("blob:already-cached");
    expect(calls.length).toBe(0);
  });

  it("clear() revokes every blob URL and notifies subscribers", async () => {
    const { fetchImpl } = makeFetch();
    const cache = new ClipCache(8, fetchImpl);
    const seen: number[] = [];
    cache.subscribe(() => seen.push(cache.version()));

    cache.prefetch("a");
    cache.prefetch("b");
    await flush();
    expect(created.length).toBe(2);

    cache.clear();
    expect(revoked.length).toBe(2);
    expect(cache.has("a")).toBe(false);
    // version bumped on each cached clip AND on clear → subscriber fired.
    expect(seen.length).toBeGreaterThanOrEqual(3);
  });

  it("falls back to the source URL when a fetch fails, and allows retry", async () => {
    let attempt = 0;
    const fetchImpl = vi.fn(async () => {
      attempt++;
      if (attempt === 1) return { ok: false, status: 500 } as unknown as Response;
      return {
        ok: true,
        status: 200,
        blob: async () => new Blob([new Uint8Array([9])], { type: "video/mp4" }),
      } as unknown as Response;
    }) as unknown as typeof fetch;
    const cache = new ClipCache(8, fetchImpl);

    cache.prefetch("https://oss/fail.mp4");
    await flush();
    // Failed → uncached → resolve returns the source URL (playable via network).
    expect(cache.has("https://oss/fail.mp4")).toBe(false);
    expect(cache.resolve("https://oss/fail.mp4")).toBe("https://oss/fail.mp4");

    // Retry succeeds → now cached.
    cache.prefetch("https://oss/fail.mp4");
    await flush();
    expect(cache.has("https://oss/fail.mp4")).toBe(true);
  });
});
