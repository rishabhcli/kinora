// The contract shared between the page and the offline service worker. Kept pure
// (no `caches`, no `self`) so both sides import the SAME message shapes + cache-key
// scheme and a test can exercise the routing without a worker. The worker file
// (public/reading-sw.js) implements the side effects; this module is the spec.
//
// Why a service worker: scrolling BACK to an earlier shot must replay instantly,
// and a reader on a flaky connection (or reading on a plane) should keep the film
// they've already passed. The in-memory ClipCache covers a session; the SW cache
// survives reloads and works offline. We cache two kinds of asset: clip mp4s and
// page-text JSON. Cache names are versioned + namespaced per book so a book change
// or a cache-format bump evicts cleanly.

export const SW_CACHE_VERSION = "v1";

/** Cache namespaces. Per-book so eviction is a single cache.delete(). */
export function clipCacheName(bookId: string): string {
  return `kinora-clips-${SW_CACHE_VERSION}-${bookId}`;
}
export function pageCacheName(bookId: string): string {
  return `kinora-pages-${SW_CACHE_VERSION}-${bookId}`;
}

/** Classify a request URL into the asset kind the SW should cache it under, or
 *  null for "don't handle" (let the network serve it normally). Decoupled from any
 *  Request object — pass the URL string. */
export type AssetKind = "clip" | "page" | null;

export function classifyAsset(url: string): AssetKind {
  // Clip mp4s (object storage or the bundled /generated films).
  if (/\.mp4(\?|$)/i.test(url) || url.includes("/generated/")) return "clip";
  // Page-text JSON from the API (…/pages/{n} or …/page/{n}).
  if (/\/pages?\/\d+(\?|$)/i.test(url)) return "page";
  return null;
}

// --- message protocol (postMessage page ⇄ worker) ------------------------

export type PageToSw =
  | { type: "PRECACHE"; bookId: string; clipUrls: string[]; pageUrls: string[] }
  | { type: "EVICT_BOOK"; bookId: string }
  | { type: "EVICT_ALL" }
  | { type: "QUERY_STATUS"; bookId: string };

export type SwToPage =
  | { type: "PRECACHE_PROGRESS"; bookId: string; done: number; total: number }
  | { type: "PRECACHE_DONE"; bookId: string; cached: number; failed: number }
  | { type: "STATUS"; bookId: string; clips: number; pages: number; bytes: number }
  | { type: "EVICTED"; scope: "book" | "all"; bookId?: string };

/** Type guards so the worker / page can narrow incoming messages safely. */
export function isPageToSw(msg: unknown): msg is PageToSw {
  if (!msg || typeof msg !== "object") return false;
  const t = (msg as { type?: unknown }).type;
  return t === "PRECACHE" || t === "EVICT_BOOK" || t === "EVICT_ALL" || t === "QUERY_STATUS";
}

export function isSwToPage(msg: unknown): msg is SwToPage {
  if (!msg || typeof msg !== "object") return false;
  const t = (msg as { type?: unknown }).type;
  return t === "PRECACHE_PROGRESS" || t === "PRECACHE_DONE" || t === "STATUS" || t === "EVICTED";
}

/** The runtime caching strategy per asset kind:
 *   - clip: cache-first (immutable bytes; once cached, never refetch — fast + offline)
 *   - page: stale-while-revalidate (serve cache instantly, refresh in the background
 *     since page text can be re-rendered by the backend)
 *  A pure descriptor the worker reads so the policy is testable here. */
export type CacheStrategy = "cache-first" | "stale-while-revalidate" | "network-only";

export function strategyFor(kind: AssetKind): CacheStrategy {
  switch (kind) {
    case "clip":
      return "cache-first";
    case "page":
      return "stale-while-revalidate";
    default:
      return "network-only";
  }
}
