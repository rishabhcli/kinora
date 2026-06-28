// Per-shot AI-clip cache for the Scroll Film Engine.
//
// The backend persists each rendered clip to object storage and re-emits its URL
// on every reconnect, and the browser HTTP cache *usually* keeps a recently
// fetched mp4 around — but neither is a guarantee. Scrolling BACK to an earlier
// shot must replay the SAME clip instantly, with no network round-trip and no
// flash of empty video. So we keep our own cache: fetch each clip's bytes once,
// hold them as a Blob, and hand out a stable `blob:` object URL that the
// <video> can replay forever without touching the network.
//
// Keyed by the resolved (browser-ready) clip URL — shots map 1:1 to a URL, so a
// URL key is equivalent to a shot key and naturally dedupes when two shots share
// a clip. Capacity-bounded (LRU): the least-recently-used blob URLs are revoked
// on eviction so a long book never leaks decoded video. The caller revokes the
// whole cache on unmount / book-change / logout via `clear()`.

interface Entry {
  /** the source (network) URL this entry was created from */
  url: string;
  /** the `blob:` object URL once the bytes are in memory; null while fetching */
  blobUrl: string | null;
  /** in-flight fetch, so concurrent callers (preload + playhead) share one request */
  inflight: Promise<string | null> | null;
}

const DEFAULT_CAPACITY = 12; // ~12 short vertical clips of decoded mp4 bytes

export class ClipCache {
  // Map iteration order is insertion order; we delete+reinsert on access to keep
  // it ordered oldest→newest, which makes the eldest key the LRU victim.
  private entries = new Map<string, Entry>();
  private capacity: number;
  private fetchImpl: typeof fetch | null;
  // Bumped whenever a clip finishes caching (or the cache is cleared), so React
  // consumers can re-resolve the timeline to pick up freshly-cached blob URLs.
  private versionN = 0;
  private listeners = new Set<() => void>();

  constructor(capacity = DEFAULT_CAPACITY, fetchImpl?: typeof fetch) {
    this.capacity = Math.max(1, capacity);
    // Bind so `this` doesn't leak into the global fetch; null in non-browser test
    // envs (jsdom has fetch, but guard anyway — then resolve() is a passthrough).
    this.fetchImpl =
      fetchImpl ?? (typeof fetch === "function" ? fetch.bind(globalThis) : null);
  }

  /** Monotonic version — changes when the set of cached blob URLs changes. */
  version(): number {
    return this.versionN;
  }

  /** Subscribe to cache changes (a clip finished caching / the cache cleared).
   *  Returns an unsubscribe fn. */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private bump(): void {
    this.versionN++;
    for (const fn of this.listeners) fn();
  }

  /** Mark `url` as the most-recently-used (move to the end of the Map order). */
  private touch(url: string): Entry | undefined {
    const e = this.entries.get(url);
    if (!e) return undefined;
    this.entries.delete(url);
    this.entries.set(url, e);
    return e;
  }

  /** Drop the least-recently-used entries until we're within capacity, revoking
   *  each evicted blob URL so the decoded bytes can be reclaimed. In-flight
   *  fetches are never evicted (they have no blob to revoke yet and a pending
   *  visitor is waiting on them). */
  private evict(): void {
    if (this.entries.size <= this.capacity) return;
    for (const [url, e] of this.entries) {
      if (this.entries.size <= this.capacity) break;
      if (e.inflight) continue; // still loading — keep it
      if (e.blobUrl) revokeObjectUrl(e.blobUrl);
      this.entries.delete(url);
    }
  }

  /** Best-effort: kick off (or join) a background fetch so a future visit is
   *  instant. No-op for an empty URL or when no fetch transport is available. */
  prefetch(url: string | null | undefined): void {
    // A blob: URL is already a cached entry's output — nothing to fetch.
    if (!url || url.startsWith("blob:") || !this.fetchImpl) return;
    void this.load(url);
  }

  /** The URL a <video> should use for `url` *right now*: the cached `blob:` URL
   *  if the bytes are in memory, otherwise the source URL (so playback still
   *  works on the very first visit via the network) — and a background fetch is
   *  started so the NEXT visit (e.g. scroll-back) replays from cache instantly.
   *  Always returns a non-empty string for a non-empty input, so callers never
   *  blank the pane. */
  resolve(url: string | null | undefined): string {
    if (!url) return "";
    if (url.startsWith("blob:")) return url; // already a cached output
    const e = this.touch(url);
    if (e?.blobUrl) return e.blobUrl;
    this.prefetch(url); // not cached yet → warm it for next time
    return url;
  }

  /** Fetch (once) and store the blob URL for `url`. Shared in-flight promise. */
  private load(url: string): Promise<string | null> {
    const existing = this.touch(url);
    if (existing) {
      if (existing.blobUrl) return Promise.resolve(existing.blobUrl);
      if (existing.inflight) return existing.inflight;
    }
    if (!this.fetchImpl) return Promise.resolve(null);

    const entry: Entry = existing ?? { url, blobUrl: null, inflight: null };
    const p = this.fetchImpl(url)
      .then((res) => {
        if (!res.ok) throw new Error(`clip fetch ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        entry.inflight = null;
        // The entry may have been cleared/evicted while fetching; only keep the
        // blob URL if the entry is still the live one for this URL.
        if (this.entries.get(url) !== entry) return null;
        entry.blobUrl = createObjectUrl(blob);
        this.evict();
        this.bump(); // a new blob URL is available → consumers re-resolve
        return entry.blobUrl;
      })
      .catch(() => {
        // Network/abort failure: forget the entry so a later visit can retry, and
        // fall back to the source URL (resolve() returns it when uncached).
        entry.inflight = null;
        if (this.entries.get(url) === entry && !entry.blobUrl) this.entries.delete(url);
        return null;
      });

    entry.inflight = p;
    if (!existing) {
      this.entries.set(url, entry);
      this.evict();
    }
    return p;
  }

  /** True once `url`'s bytes are cached and replay is network-free. */
  has(url: string | null | undefined): boolean {
    return Boolean(url && this.entries.get(url)?.blobUrl);
  }

  /** Revoke every blob URL and drop all entries (unmount / book-change / logout).
   *  In-flight fetches are detached (their late resolution finds no live entry
   *  and revokes its own blob), so nothing leaks. */
  clear(): void {
    for (const e of this.entries.values()) {
      if (e.blobUrl) revokeObjectUrl(e.blobUrl);
    }
    this.entries.clear();
    this.bump();
  }
}

function createObjectUrl(blob: Blob): string {
  const u = globalThis.URL;
  return u && typeof u.createObjectURL === "function" ? u.createObjectURL(blob) : "";
}

function revokeObjectUrl(blobUrl: string): void {
  const u = globalThis.URL;
  if (u && typeof u.revokeObjectURL === "function" && blobUrl.startsWith("blob:")) {
    try {
      u.revokeObjectURL(blobUrl);
    } catch {
      /* already revoked / unsupported */
    }
  }
}
