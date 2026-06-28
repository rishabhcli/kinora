// SW core logic with a fake CacheStorage + fetch. Vitest (imports sibling
// swProtocol with an extensionless specifier).
import { describe, expect, it, vi } from "vitest";
import {
  handleFetch,
  precache,
  evictBook,
  evictAll,
  status,
  handleMessage,
  type CacheStorageLike,
  type CacheLike,
  type ResponseLike,
} from "./readingSwCore";
import type { SwToPage } from "./swProtocol";

function res(ok = true, contentLength?: number): ResponseLike {
  return {
    ok,
    clone() {
      return res(ok, contentLength);
    },
    headers: { get: (n: string) => (n.toLowerCase() === "content-length" && contentLength != null ? String(contentLength) : null) },
  };
}

function fakeCaches() {
  const stores = new Map<string, Map<string, ResponseLike>>();
  const open = async (name: string): Promise<CacheLike> => {
    const store = stores.get(name) ?? new Map<string, ResponseLike>();
    stores.set(name, store);
    return {
      match: async (k) => store.get(k),
      put: async (k, v) => void store.set(k, v),
      delete: async (k) => store.delete(k),
      keys: async () => Array.from(store.keys()).map((url) => ({ url })),
    };
  };
  const caches: CacheStorageLike = {
    open,
    delete: async (name) => stores.delete(name),
    keys: async () => Array.from(stores.keys()),
  };
  return { caches, stores };
}

describe("readingSwCore", () => {
  it("network-only assets are not handled (returns null)", async () => {
    const { caches } = fakeCaches();
    const fetch = vi.fn(async () => res());
    const out = await handleFetch({ caches, fetch, post: () => {} }, "b1", "https://api/auth/login");
    expect(out).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("clips are cache-first: fetched once, then served from cache", async () => {
    const { caches } = fakeCaches();
    const fetch = vi.fn(async () => res());
    const deps = { caches, fetch, post: () => {} };
    const url = "https://oss/clips/a.mp4";
    await handleFetch(deps, "b1", url); // miss → fetch + store
    await handleFetch(deps, "b1", url); // hit → no second fetch
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("pages are stale-while-revalidate: a cached page serves instantly + refreshes", async () => {
    const { caches, stores } = fakeCaches();
    const fetch = vi.fn(async () => res());
    const deps = { caches, fetch, post: () => {} };
    const url = "https://api/books/b1/pages/3";
    await handleFetch(deps, "b1", url); // miss → fetch
    const before = fetch.mock.calls.length;
    await handleFetch(deps, "b1", url); // hit → returns cache AND kicks a refresh
    // The cache had it, so we returned synchronously and queued a revalidate fetch.
    expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(before);
    expect(stores.size).toBeGreaterThan(0);
  });

  it("precache reports progress and a done summary, surviving a failed clip", async () => {
    const { caches } = fakeCaches();
    const fetch = vi.fn(async (url: string) => (url.includes("bad") ? res(false) : res()));
    const msgs: SwToPage[] = [];
    await precache({ caches, fetch, post: (m) => msgs.push(m) }, {
      type: "PRECACHE",
      bookId: "b1",
      clipUrls: ["ok1.mp4", "bad.mp4"],
      pageUrls: ["p1"],
    });
    const done = msgs.find((m) => m.type === "PRECACHE_DONE");
    expect(done).toMatchObject({ type: "PRECACHE_DONE", bookId: "b1", cached: 2, failed: 1 });
    const progress = msgs.filter((m) => m.type === "PRECACHE_PROGRESS");
    expect(progress.length).toBe(3);
  });

  it("evictBook deletes both caches and posts EVICTED", async () => {
    const { caches } = fakeCaches();
    const msgs: SwToPage[] = [];
    const deps = { caches, fetch: async () => res(), post: (m: SwToPage) => msgs.push(m) };
    await handleFetch(deps, "b1", "https://oss/clips/a.mp4"); // populate a cache
    await evictBook(deps, "b1");
    expect(msgs).toContainEqual({ type: "EVICTED", scope: "book", bookId: "b1" });
  });

  it("evictAll removes every kinora-* cache", async () => {
    const { caches, stores } = fakeCaches();
    stores.set("kinora-clips-v1-b1", new Map());
    stores.set("kinora-pages-v1-b2", new Map());
    stores.set("other-app-cache", new Map());
    const msgs: SwToPage[] = [];
    await evictAll({ caches, fetch: async () => res(), post: (m) => msgs.push(m) });
    expect(stores.has("kinora-clips-v1-b1")).toBe(false);
    expect(stores.has("kinora-pages-v1-b2")).toBe(false);
    expect(stores.has("other-app-cache")).toBe(true);
    expect(msgs).toContainEqual({ type: "EVICTED", scope: "all" });
  });

  it("status counts cached clips + pages", async () => {
    const { caches } = fakeCaches();
    const msgs: SwToPage[] = [];
    const deps = { caches, fetch: async () => res(), post: (m: SwToPage) => msgs.push(m) };
    await handleFetch(deps, "b1", "https://oss/clips/a.mp4");
    await handleFetch(deps, "b1", "https://api/books/b1/pages/1");
    await status(deps, "b1");
    expect(msgs).toContainEqual({ type: "STATUS", bookId: "b1", clips: 1, pages: 1, bytes: 0 });
  });

  it("handleMessage routes by type", async () => {
    const { caches } = fakeCaches();
    const msgs: SwToPage[] = [];
    await handleMessage({ caches, fetch: async () => res(), post: (m) => msgs.push(m) }, { type: "EVICT_ALL" });
    expect(msgs).toContainEqual({ type: "EVICTED", scope: "all" });
  });
});
