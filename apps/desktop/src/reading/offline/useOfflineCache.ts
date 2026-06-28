// The page-side hook for the offline cache. It registers the reading service
// worker (when the platform supports it), forwards a precache manifest to it, and
// surfaces the precache progress + cache status so the shell can show a
// "downloaded for offline" affordance. It is entirely best-effort: in Electron's
// file:// renderer (no SW scope) or any environment without
// `navigator.serviceWorker`, the hook silently no-ops and the in-memory ClipCache
// still covers the session. Nothing here is on the playback hot path.
//
// The worker's logic lives in readingSwCore.ts (tested); the deployed worker file
// (Phase 7) is a thin entrypoint that imports it. This hook only speaks the
// swProtocol message contract.
import { useCallback, useEffect, useRef, useState } from "react";
import { isSwToPage, type PageToSw, type SwToPage } from "./swProtocol";
import type { PrecacheManifest } from "./manifest";

export interface OfflineStatus {
  /** is a controlling service worker available? */
  supported: boolean;
  /** registered + controlling this page */
  active: boolean;
  /** precache progress 0..1 (null when idle) */
  progress: number | null;
  /** cached counts for the current book */
  clips: number;
  pages: number;
  /** last error note, if any */
  error: string | null;
}

const IDLE: OfflineStatus = {
  supported: false,
  active: false,
  progress: null,
  clips: 0,
  pages: 0,
  error: null,
};

export interface UseOfflineCacheOptions {
  /** path the worker is served from (default: a same-origin /reading-sw.js) */
  scriptUrl?: string;
  /** turn the whole feature off (e.g. a setting) */
  enabled?: boolean;
}

interface SwController {
  postMessage(msg: PageToSw): void;
}

export interface OfflineCache {
  status: OfflineStatus;
  /** ask the worker to precache a book's manifest */
  precache(manifest: PrecacheManifest): void;
  /** drop a book's offline cache */
  evictBook(bookId: string): void;
  /** drop everything (logout) */
  evictAll(): void;
}

export function useOfflineCache(options: UseOfflineCacheOptions = {}): OfflineCache {
  const { scriptUrl = "/reading-sw.js", enabled = true } = options;
  const [status, setStatus] = useState<OfflineStatus>(IDLE);
  const controllerRef = useRef<SwController | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const nav = typeof navigator !== "undefined" ? (navigator as Navigator & { serviceWorker?: ServiceWorkerContainer }) : undefined;
    const sw = nav?.serviceWorker;
    if (!sw || typeof sw.register !== "function") {
      setStatus((s) => ({ ...s, supported: false }));
      return;
    }
    setStatus((s) => ({ ...s, supported: true }));

    let alive = true;
    const onMessage = (e: MessageEvent) => {
      if (!alive || !isSwToPage(e.data)) return;
      applyMessage(e.data, setStatus);
    };
    sw.addEventListener("message", onMessage);

    sw.register(scriptUrl).then(
      (reg) => {
        if (!alive) return;
        const active = (reg.active ?? sw.controller) as unknown as SwController | null;
        controllerRef.current = active ?? null;
        setStatus((s) => ({ ...s, active: Boolean(active) }));
      },
      (err: unknown) => {
        if (alive) setStatus((s) => ({ ...s, error: String(err) }));
      },
    );

    return () => {
      alive = false;
      sw.removeEventListener("message", onMessage);
    };
  }, [enabled, scriptUrl]);

  const post = useCallback((msg: PageToSw) => {
    const nav = typeof navigator !== "undefined" ? (navigator as Navigator & { serviceWorker?: ServiceWorkerContainer }) : undefined;
    const target = controllerRef.current ?? (nav?.serviceWorker?.controller as unknown as SwController | null);
    target?.postMessage(msg);
  }, []);

  const precache = useCallback(
    (manifest: PrecacheManifest) => {
      setStatus((s) => ({ ...s, progress: 0 }));
      post({ type: "PRECACHE", bookId: manifest.bookId, clipUrls: manifest.clipUrls, pageUrls: manifest.pageUrls });
    },
    [post],
  );
  const evictBook = useCallback((bookId: string) => post({ type: "EVICT_BOOK", bookId }), [post]);
  const evictAll = useCallback(() => post({ type: "EVICT_ALL" }), [post]);

  return { status, precache, evictBook, evictAll };
}

/** Pure reducer for an incoming SwToPage message → next status. Exported for tests. */
export function applyMessage(msg: SwToPage, setStatus: (fn: (s: OfflineStatus) => OfflineStatus) => void): void {
  switch (msg.type) {
    case "PRECACHE_PROGRESS":
      setStatus((s) => ({ ...s, progress: msg.total > 0 ? msg.done / msg.total : 1 }));
      break;
    case "PRECACHE_DONE":
      setStatus((s) => ({ ...s, progress: 1 }));
      break;
    case "STATUS":
      setStatus((s) => ({ ...s, clips: msg.clips, pages: msg.pages }));
      break;
    case "EVICTED":
      setStatus((s) => ({ ...s, clips: 0, pages: 0, progress: null }));
      break;
  }
}
