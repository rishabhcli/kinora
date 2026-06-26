// Cover cache: on successful auth we warm the browser image cache with the
// library's HD covers so BookWall + the post-login library feel instant — and so
// the demo library is browsable offline once seen. Pure list-building is split
// from the (browser-only) prefetch so it stays unit-testable under node --test.
//
// SOURCE NOTE (Agent 5 seam): getCoverUrls() currently reads cover URLs from the
// local demo `data/books`. At integration, Agent 12 points this at Agent 5's HD
// cover/thumbnail API. Callers don't change.

import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
} from "../../data/books.ts";

interface HasCover {
  coverImage?: string;
}

/** Deduped, non-empty, order-preserving, capped list of cover URLs to prefetch. */
export function coverPrefetchList(books: readonly HasCover[], limit = 24): string[] {
  if (!Array.isArray(books)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const b of books) {
    const url = b?.coverImage;
    if (!url || seen.has(url)) continue;
    seen.add(url);
    out.push(url);
    if (out.length >= limit) break;
  }
  return out;
}

/** Cover URLs for the signed-in library. Stubbed to the local demo catalogue
 *  today (Agent 5's cover API slots in here at integration). Never throws. */
export async function getCoverUrls(limit = 24): Promise<string[]> {
  const pool = [
    ...continueReading,
    ...recentlyAdded,
    ...popularOnKinora,
    ...recommended,
    ...awardWinners,
  ];
  return coverPrefetchList(pool, limit);
}

/** Warm the browser image cache. Fire-and-forget, browser-only, offline-safe —
 *  decode failures (e.g. backend unreachable) are swallowed so login never blocks. */
export function warmCoverCache(urls: readonly string[]): void {
  if (typeof window === "undefined" || typeof Image === "undefined") return;
  for (const url of urls) {
    try {
      const img = new Image();
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.src = url;
    } catch {
      /* ignore — prefetch is best-effort */
    }
  }
}

/** Convenience: resolve the library covers and warm them. Safe to await or not. */
export async function warmLibraryCovers(limit = 24): Promise<void> {
  warmCoverCache(await getCoverUrls(limit));
}
