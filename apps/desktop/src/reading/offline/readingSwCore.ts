// The service-worker LOGIC, written as a pure-ish core that takes an injected
// CacheStorage-like + fetch so it is unit-testable without a real ServiceWorker
// global. The thin worker entrypoint (Phase 7: public/reading-sw.js) wires this to
// the real `self`/`caches`/`fetch`. Keeping the logic here (in my domain) means
// the fetch routing, the cache strategies, precache progress, and eviction are all
// tested; the entrypoint is a few lines of glue.

import {
  classifyAsset,
  clipCacheName,
  pageCacheName,
  strategyFor,
  type AssetKind,
  type PageToSw,
  type SwToPage,
} from "./swProtocol";

// Minimal structural shapes of the Cache Storage API (so tests pass a fake).
export interface CacheLike {
  match(req: string): Promise<ResponseLike | undefined>;
  put(req: string, res: ResponseLike): Promise<void>;
  delete(req: string): Promise<boolean>;
  keys(): Promise<{ url: string }[]>;
}
export interface CacheStorageLike {
  open(name: string): Promise<CacheLike>;
  delete(name: string): Promise<boolean>;
  keys(): Promise<string[]>;
}
export interface ResponseLike {
  ok: boolean;
  clone(): ResponseLike;
  headers?: { get(name: string): string | null };
}

type FetchLike = (url: string) => Promise<ResponseLike>;

export interface SwCoreDeps {
  caches: CacheStorageLike;
  fetch: FetchLike;
  /** post a message back to the page(s) */
  post: (msg: SwToPage) => void;
}

/** Handle a fetch for `url` per the asset's strategy. Returns the Response to
 *  serve, or null when the SW shouldn't handle it (caller falls back to network).
 *  Never throws — a cache miss + network failure returns null so the page's own
 *  error handling (which already degrades to the fallback film) takes over. */
export async function handleFetch(deps: SwCoreDeps, bookId: string, url: string): Promise<ResponseLike | null> {
  const kind = classifyAsset(url);
  const strategy = strategyFor(kind);
  if (strategy === "network-only" || kind == null) return null;

  const cacheName = kind === "clip" ? clipCacheName(bookId) : pageCacheName(bookId);
  const cache = await deps.caches.open(cacheName);

  if (strategy === "cache-first") {
    const hit = await cache.match(url);
    if (hit) return hit;
    const res = await safeFetch(deps.fetch, url);
    if (res && res.ok) await cache.put(url, res.clone());
    return res;
  }

  // stale-while-revalidate: serve cache now, refresh in the background.
  const hit = await cache.match(url);
  const revalidate = (async () => {
    const res = await safeFetch(deps.fetch, url);
    if (res && res.ok) await cache.put(url, res.clone());
    return res;
  })();
  if (hit) {
    void revalidate; // fire-and-forget background refresh
    return hit;
  }
  return revalidate;
}

/** Precache a book's manifest, reporting progress. Resilient: a single clip's
 *  failure doesn't abort the batch (it's counted as failed). */
export async function precache(deps: SwCoreDeps, msg: Extract<PageToSw, { type: "PRECACHE" }>): Promise<void> {
  const { bookId, clipUrls, pageUrls } = msg;
  const items: { url: string; kind: AssetKind }[] = [
    ...clipUrls.map((url) => ({ url, kind: "clip" as AssetKind })),
    ...pageUrls.map((url) => ({ url, kind: "page" as AssetKind })),
  ];
  const total = items.length;
  let done = 0;
  let cached = 0;
  let failed = 0;
  for (const item of items) {
    const name = item.kind === "clip" ? clipCacheName(bookId) : pageCacheName(bookId);
    try {
      const cache = await deps.caches.open(name);
      const existing = await cache.match(item.url);
      if (existing) {
        cached++;
      } else {
        const res = await deps.fetch(item.url);
        if (res.ok) {
          await cache.put(item.url, res.clone());
          cached++;
        } else {
          failed++;
        }
      }
    } catch {
      failed++;
    }
    done++;
    deps.post({ type: "PRECACHE_PROGRESS", bookId, done, total });
  }
  deps.post({ type: "PRECACHE_DONE", bookId, cached, failed });
}

/** Delete a book's clip + page caches. */
export async function evictBook(deps: SwCoreDeps, bookId: string): Promise<void> {
  await deps.caches.delete(clipCacheName(bookId));
  await deps.caches.delete(pageCacheName(bookId));
  deps.post({ type: "EVICTED", scope: "book", bookId });
}

/** Delete every kinora cache (logout / format bump). */
export async function evictAll(deps: SwCoreDeps): Promise<void> {
  const names = await deps.caches.keys();
  for (const n of names) {
    if (n.startsWith("kinora-")) await deps.caches.delete(n);
  }
  deps.post({ type: "EVICTED", scope: "all" });
}

/** Count cached clips + pages for the status message. */
export async function status(deps: SwCoreDeps, bookId: string): Promise<void> {
  const clip = await deps.caches.open(clipCacheName(bookId));
  const page = await deps.caches.open(pageCacheName(bookId));
  const clips = (await clip.keys()).length;
  const pages = (await page.keys()).length;
  deps.post({ type: "STATUS", bookId, clips, pages, bytes: 0 });
}

/** Route an incoming page→worker message to the right handler. */
export async function handleMessage(deps: SwCoreDeps, msg: PageToSw): Promise<void> {
  switch (msg.type) {
    case "PRECACHE":
      return precache(deps, msg);
    case "EVICT_BOOK":
      return evictBook(deps, msg.bookId);
    case "EVICT_ALL":
      return evictAll(deps);
    case "QUERY_STATUS":
      return status(deps, msg.bookId);
  }
}

async function safeFetch(fetchLike: FetchLike, url: string): Promise<ResponseLike | null> {
  try {
    return await fetchLike(url);
  } catch {
    return null;
  }
}
