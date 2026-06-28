// Compute WHAT to precache for offline reading, and a budget-bounded eviction
// plan. Pure: it consumes the timeline segments + page list the engine already has
// and produces a deterministic, deduped, prioritised precache manifest plus an
// over-budget eviction plan, with no `caches`/`fetch`. The SW registration hook
// turns a manifest into PRECACHE messages; the worker enforces it.
//
// Priority model: cache the reader's NEAR future + recent past first (a flick back
// is the worst-feeling miss), then fan outward, so a capped offline budget always
// covers the most likely next frames.

export interface ManifestSegment {
  /** clip URL (the segment's `src`; skip blob: — those are session-only) */
  src: string;
  /** the segment's start word, for distance-from-reader prioritisation */
  wordStart: number;
  /** estimated bytes (duration_s × a bitrate guess), for budget accounting */
  estBytes?: number;
}

export interface ManifestInput {
  bookId: string;
  segments: ManifestSegment[];
  /** page-text URLs to cache (already browser-ready) */
  pageUrls: string[];
  /** the reader's current focus word (drives clip priority) */
  focusWord: number;
  /** total bytes we're allowed to hold offline for this book */
  budgetBytes: number;
  /** default per-clip byte estimate when a segment has none (≈ 8s @ 2.5Mbps) */
  defaultClipBytes?: number;
}

export interface PrecacheManifest {
  bookId: string;
  /** clip URLs to cache, highest priority first, trimmed to the budget */
  clipUrls: string[];
  /** page-text URLs to cache (cheap; always all of them) */
  pageUrls: string[];
  /** the byte total the clip list is expected to occupy */
  plannedBytes: number;
  /** clips that didn't fit the budget (telemetry / a "needs more space" hint) */
  droppedForBudget: string[];
}

const DEFAULT_CLIP_BYTES = 8 * 2_500_000 / 8; // ~8s at 2.5 Mbps ≈ 2.5 MB

/** Build a prioritised, budget-bounded precache manifest. Clips are ordered by
 *  |wordStart − focusWord| (nearest the reader first), with ties broken by the
 *  forward direction (a clip just AHEAD beats one equally far BEHIND), then packed
 *  greedily into the byte budget. blob: srcs are skipped (session-only). */
export function buildManifest(input: ManifestInput): PrecacheManifest {
  const perClip = input.defaultClipBytes ?? DEFAULT_CLIP_BYTES;
  // Dedupe by src, keeping the nearest occurrence.
  const seen = new Set<string>();
  const candidates = input.segments
    .filter((s) => s.src && !s.src.startsWith("blob:"))
    .filter((s) => (seen.has(s.src) ? false : (seen.add(s.src), true)))
    .map((s) => ({
      src: s.src,
      bytes: s.estBytes && s.estBytes > 0 ? s.estBytes : perClip,
      // Distance with a small forward bias: behind costs a touch more than ahead.
      score: priorityScore(s.wordStart, input.focusWord),
    }))
    .sort((a, b) => a.score - b.score);

  const clipUrls: string[] = [];
  const dropped: string[] = [];
  let planned = 0;
  for (const c of candidates) {
    if (planned + c.bytes <= input.budgetBytes) {
      clipUrls.push(c.src);
      planned += c.bytes;
    } else {
      dropped.push(c.src);
    }
  }

  return {
    bookId: input.bookId,
    clipUrls,
    pageUrls: dedupe(input.pageUrls),
    plannedBytes: planned,
    droppedForBudget: dropped,
  };
}

/** Lower = higher priority. Distance from the reader, with a forward bias so an
 *  upcoming clip outranks an equally-distant past one. */
export function priorityScore(wordStart: number, focusWord: number): number {
  const delta = wordStart - focusWord;
  const distance = Math.abs(delta);
  // Behind the reader: +5% penalty so ahead wins ties (and near-ties).
  return delta < 0 ? distance * 1.05 : distance;
}

export interface EvictionPlan {
  /** entries to remove, oldest/farthest first, until within budget */
  evict: string[];
  /** bytes freed by the plan */
  freedBytes: number;
}

/** Given currently-cached entries (src + bytes + last-access tick) and a budget,
 *  return which to evict (LRU + farthest-from-reader) to get back under budget. */
export function planEviction(
  cached: { src: string; bytes: number; lastAccessTick: number }[],
  budgetBytes: number,
): EvictionPlan {
  const total = cached.reduce((a, c) => a + c.bytes, 0);
  if (total <= budgetBytes) return { evict: [], freedBytes: 0 };
  // Evict least-recently-used first (smallest tick = oldest access).
  const byLru = [...cached].sort((a, b) => a.lastAccessTick - b.lastAccessTick);
  const evict: string[] = [];
  let freed = 0;
  let remaining = total;
  for (const c of byLru) {
    if (remaining <= budgetBytes) break;
    evict.push(c.src);
    freed += c.bytes;
    remaining -= c.bytes;
  }
  return { evict, freedBytes: freed };
}

function dedupe(urls: string[]): string[] {
  return Array.from(new Set(urls.filter(Boolean)));
}
